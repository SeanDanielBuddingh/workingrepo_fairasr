#!/usr/bin/env python3

import sys
import torch
import torch.nn as nn
import logging
import speechbrain as sb
import torchaudio
from hyperpyyaml import load_hyperpyyaml
from mySentencePiece import SentencePiece
from speechbrain.utils.data_utils import undo_padding
from speechbrain.utils.distributed import run_on_main, if_main_process
import warnings
import yaml
import sentencepiece as spm
import wandb
from mySchedulers import MyIntervalScheduler
#from speechbrain.tokenizers.SentencePiece import SentencePiece
#from pyctcdecode import build_ctcdecoder

import random
import csv
from speechbrain.dataio.dataset import DynamicItemDataset

import math

logger = logging.getLogger(__name__)

# Define training procedure

class ASR(sb.core.Brain):
    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""

        batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        tokens_bos, _ = batch.tokens_bos
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)

        if stage == sb.Stage.TRAIN:
            if hasattr(self.hparams, "augmentation"):
                wavs = self.hparams.augmentation(wavs, wav_lens)

        # Forward pass
        feats = self.modules.wav2vec2(wavs, wav_lens)
        logits = self.modules.ctc_lin(feats) # x
        p_ctc = self.hparams.log_softmax(logits)

        return p_ctc, wav_lens

    ###GCR begin~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #not used at the moment >> Contrastive loss minimizes distance between in class representations, but in ASR, there are no distinct classes >> maybe we can make some according to accents.
    def l_supcon(self, x, y, A, theta, omega_theta):
        """
        Calculate the supervised contrastive loss as per Equation 6.
        
        :param x: Input sample.
        :param y: Ground truth label for the input sample.
        :param A: A set containing data points.
        :param theta: Model parameters.
        :param omega_theta: Feature extractor output function.
        :return: Supervised contrastive loss value.
        """
        P_y = [(x_dot, y_dot, z_dot) for x_dot, y_dot, z_dot in A if y_dot == y]
        #numerator = math.exp(omega_theta(x_dot) @ omega_theta(x)) for x_dot, y_dot, z_dot in P_y
        denominator = sum(math.exp(omega_theta(x_bar) @ omega_theta(x)) for x_bar, y_bar, z_bar in A)
        
        #return 1 / abs(P_y) * (numerator / denominator).sum()
    
    def L_theta(self, Dt, Xt_Ct, alpha=0.01, beta=0.01, gamma=0.01):
        """
        Calculate the overall loss L(θ) as per Equation 5.
        
        :param theta: Model parameters.
        :param Dt: Current task data.
        :param Xt_Ct: Combined data from replay buffer (Xt-1) and current task candidate pool (Ct).
        :param alpha, beta, gamma: Hyperparameters.
        :param f_theta: Model's predicted probability distribution function.
        :param h_theta: Model's logits output function.
        :param l_supcon: Supervised contrastive loss function.
        :return: Computed loss value.
        """
        predictions = self.compute_forward(Dt, sb.Stage.TRAIN)
        with torch.no_grad():
            loss = self.compute_objectives(predictions, Dt, sb.Stage.TRAIN)

        # Component (a) - Distillation Loss
        for x, y, z, w in Xt_Ct:
            loss += alpha * w * (z - self(x))**2

        # Component (b) - Label Loss
        for x_hat, y_hat, z_hat, w_hat in Xt_Ct:
            loss += beta * w_hat * self.compute_objective(predictions, (x_hat, y_hat), sb.Stage.VALID)

        #no classes, maybe constrastive losses according to accents?
        '''
        # Component (c) - Supervised Contrastive Loss
        for x_tilde, y_tilde, z_tilde, w_tilde in Xt_Ct:
            loss += gamma * w_tilde * self.l_supcon(x_tilde, y_tilde, Xt_Ct, theta)
        '''
        loss.backward()

        return loss
    
    def gcr_obj(self, D, WD, X, WX, lambda_):
        L_sub_d = self.L_theta(D, WD) #should be gradients 
        L_sub_r = self.L_theta(X, WX) #should be gradients

        gcr_obj = torch.sqrt(((L_sub_d.sum() - L_sub_r.sum())**2).sum()) - (lambda_ * torch.sqrt((WX**2).sum()))
        return gcr_obj

    def gradprox(self, D, WD, lambda_, K, tolerance):
        theta = self.parameters()

        Y = len(D)
        #D_partitioned = self.partition_by_accent(D)
        #WD_partitioned = self.partition_by_accent(WD)
        X, WX = set(), []

        for y in range(1, Y + 1):
            ky = K // Y
            Xy, WXy = set(), []
            l_sub = self.gcr_obj(D, WD, Xy, WXy, lambda_)
            r = l_sub #change to gradients
            
            while len(Xy) <= ky and l_sub >= tolerance:
                e = torch.argmax(r)
                Xy.add(e)
                WXy = 0#argmin l_sub >> Implement this
                r = l_sub #change to gradients #same as line above

                X.update(Xy)
                WX.extend(WXy)

        return X, WX

    def partition_by_accent(self, D):
        partitioned_data = {}
        for item in D:
            accent = item["accents_field"]  # Get the accent for the item
            if accent not in partitioned_data:
                partitioned_data[accent] = []
            partitioned_data[accent].append(item)
        return partitioned_data

    def GCR_algorithm(self, D, theta, alpha, beta, gamma, lambda_, learning_rate, batch_size, buffer_size, tolerance):
        # Initialize Replay Buffer
        X = set()  # Replay Buffer
        WX = {}  # Replay Buffer Weights
        n = 0  # Sample Count

        for D_t in D:
            # Initialize Candidate Pool
            C_t = set()
            n_t = 0  # Task Sample Count

            for (x, y) in D_t:
                # Update Task Sample Count
                n_t += 1
                n += 1

                # Adaptive Sampling (function definition not provided in the algorithm)
                x_prime, y_prime, z_prime, w_prime = self.adaptive_sampling(X, WX, C_t, n_t, n)

                # Data Augmentation >> Unused
                #x_aug = self.augment(x)
                #x_prime_aug = self.augment(x_prime)
            
                z = self.compute_forward(x, sb.Stage.VALID)

                # Update Parameters
                theta -= learning_rate * self.L_theta(Dt=D_t, Xt_Ct=(x_prime, y_prime, z_prime, w_prime))

                # Update Candidate Pool (Reservoir function definition not provided in the algorithm)
                C_t = Reservoir(C_t, (x, y, z), buffer_size)

            X, WX = self.gradprox(X.union(C_t), WX.union({1}), theta, lambda_, buffer_size)

    def adaptive_sampling(X, WX, C_t, n_t, n):
        """
        Adaptive Sampling Algorithm.

        :param X: Replay Buffer
        :param W: Replay Buffer weights
        :param C_t: Candidate Pool
        :param n_t: Task sample count
        :param n: Entire sample count
        :return: Data sample (x, y, z, w)
        """
        # Calculate probability
        p = n_t / n

        # Sample a random number
        pf = random.uniform(0, 1)

        # Choose from Candidate Pool or Replay Buffer based on probability
        if pf <= p:
            # Sample from Candidate Pool
            i = random.randint(0, len(C_t) - 1)
            x, y, z = C_t[i]
            w = 1
        else:
            # Sample from Replay Buffer
            i = random.randint(0, len(X) - 1)
            x, y, z = X[i]
            w = WX[i]

        return x, y, z, w

    ##GCR end~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    
    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss (CTC) given predictions and targets."""

        p_ctc, wav_lens = predictions
        
        
        ids = batch.id
        tokens_eos, tokens_eos_lens = batch.tokens_eos
        tokens, tokens_lens = batch.tokens

        loss = self.hparams.ctc_cost(p_ctc, tokens, wav_lens, tokens_lens)

        if stage != sb.Stage.TRAIN:
            # Decode token terms to words

            sequence = sb.decoders.ctc_greedy_decode(
                p_ctc, wav_lens, blank_id=-1
            )
            
            """
            # Beam Search Decoding
                
            p_ctc = p_ctc.detach().cpu().numpy()
            sequence = self.beam_search_decoder.decode_batch(pool=None, logits_list=p_ctc,
                                                             beam_width=self.hparams.beam_size)
            # pool: multiprocessing pool for parallel execution

            """
            
            predicted_words = self.tokenizer(sequence, task="decode_from_list")

            
            # Convert indices to words
            target_words = undo_padding(tokens, tokens_lens)
            target_words = self.tokenizer(target_words, task="decode_from_list")
            
            
            """
            # Print at Validation stage
            
            print("target / greedy predicted words:\n")
            for i in range(2):
                print(target_words[i])
                print(predicted_words[i])
                print("\n\n")
            """

            self.wer_metric.append(ids, predicted_words, target_words)
            self.cer_metric.append(ids, predicted_words, target_words)

        return loss

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""
        
        
        should_step = self.step % self.grad_accumulation_factor == 0
        
        is_freeze_step = self.optimizer_step <= self.hparams.freeze_steps
        
        # Managing automatic mixed precision
        # TOFIX: CTC fine-tuning currently is unstable
        # This is certainly due to CTC being done in fp16 instead of fp32
            
        if self.auto_mix_prec:
            with torch.cuda.amp.autocast():
                with self.no_sync():
                    outputs = self.compute_forward(batch, sb.Stage.TRAIN)
                loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)

            with self.no_sync(not should_step):
                self.scaler.scale(
                    loss / self.grad_accumulation_factor
                ).backward()
            if should_step:
                if not self.hparams.wav2vec2.freeze:
                    self.scaler.unscale_(self.wav2vec_optimizer)
                self.scaler.unscale_(self.model_optimizer)
                if self.check_gradients(loss):
                    if not self.hparams.wav2vec2.freeze:
                        if self.optimizer_step >= self.hparams.warmup_steps:
                            self.scaler.step(self.wav2vec_optimizer)
                    self.scaler.step(self.model_optimizer)
                self.scaler.update()
                self.zero_grad()
                self.optimizer_step += 1
                
                
                
                
        else:
            # This is mandatory because HF models have a weird behavior with DDP
            # on the forward pass
            with self.no_sync():
                outputs = self.compute_forward(batch, sb.Stage.TRAIN)

            loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)

            #wandb.log({"Training loss": loss})
                
            with self.no_sync(not should_step):
                (loss / self.grad_accumulation_factor).backward()
                
                
            if should_step: ## accumulation done
                if self.check_gradients(loss):
                    if is_freeze_step:
                        self.modules.wav2vec2.freeze = True
                        
                        for param in self.modules["wav2vec2"].parameters():
                            param.requires_grad = False

                    else:
                        self.modules.wav2vec2.freeze = False
                        
                        for param in self.modules["wav2vec2"].parameters():
                            param.requires_grad = True
                    
                    self.model_optimizer.step()
                self.zero_grad()
                self.optimizer_step += 1
        
        self.on_fit_batch_end(batch, outputs, loss, should_step)
        return loss.detach().cpu()

    def on_fit_batch_end(self, batch, outputs, loss, should_step):
        """Called after ``fit_batch()``, meant for calculating and logging metrics.

        Arguments
        ---------
        batch : list of torch.Tensors
            Batch of data to use for training. Default implementation assumes
            this batch has two elements: inputs and targets.
        outputs : list or dictionary of torch.Tensors
            Returned value of compute_forward().
        loss : torch.Tensor
            Returned value of compute_objectives().
        should_step : boolean
            Whether optimizer.step() was called or not.
        """
        
        # after each step (after accumulated enough gradient and finally updated optimizer)
        if should_step:
            
            old_lr, new_lr = self.lr_annealing_model(
                    self.optimizer_step, self.model_optimizer
                )

            sb.nnet.schedulers.update_learning_rate(
                    self.model_optimizer, new_lr
                )
            
            #wandb.log({"Learning rate": old_lr})
            
            #self.hparams.train_logger.log_stats(
            #    stats_meta={
            #        "lr_model": old_lr,
            #    }
            #)
        
    
    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        predictions = self.compute_forward(batch, stage=stage)
        with torch.no_grad():
            loss = self.compute_objectives(predictions, batch, stage=stage)
        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        if stage != sb.Stage.TRAIN:
            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.error_rate_computer()
    
    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of an epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:

            self.hparams.train_logger.log_stats(
                stats_meta={
                    "epoch": epoch,
                },
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
            
            ckpt_name = "_END OF EPOCH_" + str(epoch)
            self.checkpointer.save_checkpoint(name = ckpt_name)
            
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            if if_main_process():
                with open(self.hparams.test_wer_file, "w") as w:
                    self.wer_metric.write_stats(w)

    def init_optimizers(self):
        "Initializes the model optimizer"

        # model optim instantiation
        self.model_optimizer = self.hparams.model_opt_class(
            self.hparams.model.parameters()
        )

        if self.checkpointer is not None:
            self.checkpointer.add_recoverable("modelopt", self.model_optimizer)
            
    def zero_grad(self, set_to_none=False):
        self.model_optimizer.zero_grad(set_to_none)
          
    
           
        


# Define custom data procedure
def dataio_prepare(hparams, tokenizer):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""

    # 1. Define datasets
    data_folder = hparams["data_folder"]

    train_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["train_csv"], replacements={"data_root": data_folder},
    )

    if hparams["sorting"] == "ascending":
        # we sort training data to speed up training and get better results.
        train_data = train_data.filtered_sorted(
            sort_key="duration",
            key_max_value={"duration": hparams["avoid_if_longer_than"]},
        )
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["dataloader_options"]["shuffle"] = False

    elif hparams["sorting"] == "descending":
        train_data = train_data.filtered_sorted(
            sort_key="duration",
            reverse=True,
            key_max_value={"duration": hparams["avoid_if_longer_than"]},
        )
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["dataloader_options"]["shuffle"] = False

    elif hparams["sorting"] == "random":
        pass

    else:
        raise NotImplementedError(
            "sorting must be random, ascending or descending"
        )

    valid_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["valid_csv"], replacements={"data_root": data_folder},
    )
    # We also sort the validation data so it is faster to validate
    valid_data = valid_data.filtered_sorted(sort_key="duration")

    test_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["test_csv"], replacements={"data_root": data_folder},
    )

    # We also sort the validation data so it is faster to validate
    test_data = test_data.filtered_sorted(sort_key="duration")

    datasets = [train_data, valid_data, test_data]

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("wav")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(wav):
        info = torchaudio.info(wav)
        sig = sb.dataio.dataio.read_audio(wav)
        resampled = torchaudio.transforms.Resample(
            info.sample_rate, hparams["sample_rate"],
        )(sig)
        return resampled

    sb.dataio.dataset.add_dynamic_item(datasets, audio_pipeline)

    # 3. Define text pipeline:
    @sb.utils.data_pipeline.takes("wrd")
    @sb.utils.data_pipeline.provides(
        "tokens_list", "tokens_bos", "tokens_eos", "tokens"
    )
    def text_pipeline(wrd):
        tokens_list = tokenizer.sp.encode_as_ids(wrd)
        yield tokens_list
        tokens_bos = torch.LongTensor([hparams["bos_index"]] + (tokens_list))
        yield tokens_bos
        tokens_eos = torch.LongTensor(tokens_list + [hparams["eos_index"]])
        yield tokens_eos
        tokens = torch.LongTensor(tokens_list)
        yield tokens

    sb.dataio.dataset.add_dynamic_item(datasets, text_pipeline)

    # 4. Set output:
    sb.dataio.dataset.set_output_keys(
        datasets, ["id", "duration", "wav", "spk_id", "wrd", "age", "gender", "accents",
                   "sig", "tokens"],
    )
    return train_data, valid_data, test_data
    
    
    

if __name__ == "__main__":

    #wandb.init(project='YOUR PROJECT NAME') # resume=True
    
    #wandb.run.name = "YOUR RUN NAME"
    #wandb.run.save()
    
    # Load hyperparameters file with command-line overrides
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])
    

    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)
        
    args = {
        "seed": hparams["seed"],
        "peak_lr": hparams["peak_lr"],
        "epochs": hparams["number_of_epochs"],
        "batch_size": hparams["batch_size"],
        "num_workers": hparams["num_workers"]
    }
    
    #wandb.config.update(args)
   
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.cuda.empty_cache()
    
    # create ddp_group with the right communication protocol
    sb.utils.distributed.ddp_init_group(run_opts)

    # Dataset preparation (parsing CommonVoice)
    from common_voice_prepare import prepare_common_voice  # noqa

    
    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )
    
    
    run_on_main(
        prepare_common_voice,
        kwargs={
            "data_folder": hparams["data_folder"],
            "save_folder": hparams["save_folder"],
            "train_tsv_file": hparams["train_tsv_file"],
            "dev_tsv_file": hparams["dev_tsv_file"],
            "test_tsv_file": hparams["test_tsv_file"],
            "accented_letters": hparams["accented_letters"],
            "language": hparams["language"],
            "skip_prep": hparams["skip_prep"],
        },
    )
    

    
    # Defining tokenizer and loading it
    tokenizer = SentencePiece(
        model_dir=hparams["save_folder"],
        vocab_size=hparams["output_neurons"],
        annotation_train=hparams["train_csv"],
        annotation_read="wrd",
        model_type=hparams["token_type"],
        character_coverage=hparams["character_coverage"],
        bos_id=hparams["bos_index"], # 1
		eos_id=hparams["eos_index"], # 2
		pad_id=hparams["pad_index"], # 3
		unk_id=hparams["unk_index"], # 4
        bos_piece=hparams["bos_piece"], # <bos>
		eos_piece=hparams["eos_piece"], # <eos>
    )
   
    # Defining scheduler 
    lr_annealing_model = MyIntervalScheduler(lr_initial = hparams["peak_lr"],
                                            n_warmup_steps = hparams["tenth_step"],
                                            anneal_steps = hparams["half_step"],
                                            anneal_rates = hparams["anneal_rate"])
    
    
    # Create the datasets objects as well as tokenization and encoding
    train_data, valid_data, test_data = dataio_prepare(hparams, tokenizer)

    # Trainer initialization
    asr_brain = ASR(
        modules=hparams["modules"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )

    # Adding objects to trainer.
    asr_brain.tokenizer = tokenizer
    
    asr_brain.lr_annealing_model = lr_annealing_model
    
    #asr_brain.checkpointer.add_recoverable("scheduler_model", asr_brain.lr_annealing_model)


    """
    For Beam-Search Decoding
    
    # specify alphabet labels as they appear in logits
    
    # The CTC target vocabulary includes 26 English characters, 
    # a space token (" "), an apostrophe ('), and a special CTC blank symbol (pad).
    
    #labels = [" ", "<bos>", "<eos>", "<pad>", "<unk>", 
    #          "E", "N", "I", "T", "A", "R", "S", "O", "H", "D",
    #          "L", "U", "C", "M", "G", "F", "B", "W", "P", "Y",
    #          "V", "K", "Z", "J", "X", "'", "Q"]

    # tokenizer의 vocab 순서와 동일하게 하는 것 중요!!
    
    #blank_index: 0
    #bos_index: 1
    #eos_index: 2
    #pad_index: 3
    #unk_index: 4
    
    # vocab: 32 개 token : a~z (26개) + space, eos, bos, pad, unk (5개) + " ' " (1개)

    #asr_brain.beam_search_decoder = build_ctcdecoder(labels)            

    """
        
    
    asr_brain.fit(
        asr_brain.hparams.epoch_counter,
        train_data,
        valid_data,
        train_loader_kwargs=hparams["dataloader_options"],
        valid_loader_kwargs=hparams["test_dataloader_options"],
    )
    
    
    # Test
    asr_brain.evaluate(
        test_data,
        min_key="WER",
        test_loader_kwargs=hparams["test_dataloader_options"],
    )
    

    
    
