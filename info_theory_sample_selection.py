#!/usr/bin/env python3
"""
Implementation of Sample Selection algorithm inspired by:
"Information-Theoretic Online Memory Selection for Continual Learning (2022)"
https://arxiv.org/pdf/2204.04763.pdf


"""
import sys
import torch
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
from train_final import ASR

import csv
from torch.utils.data import DataLoader
#from speechbrain.dataio.dataloader import LoopedLoader
#from speechbrain.dataio.dataset import DynamicItemDataset
import itertools
import random
import os
from torch.distributions import Categorical
from scipy.stats import norm
import math
import numpy as np

logger = logging.getLogger(__name__)

           
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
        #csv: "ID", "duration", "wav-경로", "spk_id", "wrd", "age", "gender", "accents"
        datasets, ["id", "duration", "wav", "spk_id", "wrd", "age", "gender", "accents",
                   "sig", "tokens_bos", "tokens_eos", "tokens"],
    )
    return train_data, valid_data, test_data
    
    
    
    
def create_csv(csv_file, reservoir):
    """    
    csv_file : str
        new csv file name 
    """

    # Stream into a .tmp file, and rename it to the real path at the end.
    csv_file_tmp = csv_file + ".tmp"

    with open(csv_file_tmp, mode="w", encoding="utf-8") as csv_f:
        csv_writer = csv.writer(
            csv_f, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL
        )

        #csv_writer.writerow(["ID", "wav", "spk_id", "wrd", "age", "gender", "accents"])
        csv_writer.writerow(["ID", "duration", "wav", "spk_id", "wrd", "age", "gender", "accents"])
        
        
        final_dict = reservoir.group_dict
        
        for group in final_dict:
            for sample_object in final_dict[group].values():
                csv_writer.writerow(
                    [
                        sample_object.id,
                        sample_object.duration,
                        sample_object.wav,
                        sample_object.spk_id,
                        sample_object.wrd,
                        sample_object.age,
                        sample_object.gender,
                        sample_object.accents
                    ]
                )
    
    os.replace(csv_file_tmp, csv_file)

    # Final prints
    msg = "%s successfully created!" % (csv_file)
    logger.info(msg)
    


        
#@dataclass        
class Sample:
    def __init__(self, id, duration, wav, spk_id, wrd, age, gender, accents, 
                 sig, tokens_bos, tokens_eos, tokens, 
                 feats, logits, softmax, loss, measure_M):
        self.id = id
        self.duration = duration
        self.wav = wav # path
        self.spk_id = spk_id
        self.wrd = wrd
        self.age = age
        self.gender = gender
        self.accents = accents
        self.sig = sig
        self.tokens_bos = tokens_bos
        self.tokens_eos = tokens_eos
        self.tokens = tokens
        
        self.feats = feats
        self.logits = logits
        self.softmax = softmax
        self.loss = loss
        self.measure_M = measure_M
        self.distance = 0
        self.similarity = 0
        
    def add_distance(self, distance_val):
        self.distance += distance_val
        
    def add_similarity(self, similarity_val):
        self.similarity += similarity_val
        
        
def dict_to_string(dictionary):
    string_representation = ""
    for key, value in dictionary.items():
        string_representation += str(key) + ": " + str(value) + ", "
    string_representation = string_representation.rstrip(", ")
    return string_representation

class Reservoir:
    def __init__(self, size, attribute, cardinality=None, ):
        
        if attribute == "age":
            self.groups = ["teens", "twenties", "thirties", "fourties", "fifties", "sixties", "seventies", "eighties", "nineties"]
        elif attribute == "gender":
            self.groups = ["female", "male", "other"]
            
        self.attribute = attribute    
        self.count_k_i = {i:0 for i in self.groups}
        
        if not cardinality == None:
            self.cardinality = cardinality # dictionary
        else:
            self.cardinality = None
            
        self.size = size
        self.group_dict = {i:dict() for i in self.groups} # 각 group의 Sample 객체 dict를 저장
        self.max_M_sample = None
        self.majority_group = None
        
        self.running_M = dict()
        self.running_mean_M = None
        self.running_std_M = None
        self.group_running_loss = {i:dict() for i in self.groups} # fourties: {i_d : loss}
        self.group_running_mean_loss = {i:0.0 for i in self.groups}
        self.group_running_std_loss = {i:0.0 for i in self.groups}
        
    
    def find_majority_group(self):
        self.majority_group = max(self.count_k_i, key=self.count_k_i.get)
        return self.majority_group
    
    def delete_sample_with_least_M(self):
        id_with_least_M = min(self.running_M, key=self.running_M.get)
        
        for group in self.groups:
            if self.group_dict[group].get(id_with_least_M):
                delete_sample_group = group
                break
        self.delete_sample(delete_sample_group, id_with_least_M)
    
    def update_M_stats(self):
        self.running_mean_M = torch.mean(torch.tensor(list(self.running_M.values())))
        self.running_std_M = torch.std(torch.tensor(list(self.running_M.values())))
        
    def update_group_loss_stats(self, group):
        """called after sample appended / removed"""
        group_loss_list = self.group_running_loss[group]
        
        self.group_running_mean_loss[group] = torch.mean(torch.tensor(list(group_loss_list.values())))
        #if self.count_k_i[group] != 1
        self.group_running_std_loss[group] = torch.std(torch.tensor(list(group_loss_list.values())))
                          
    def update_count_k_i(self):
        """updates count_k_i & updates majority_group"""
        for key, value in self.group_dict.items():
            self.count_k_i[key] = len([item for item in value if item])
        self.find_majority_group()
        
    def delete_sample(self, group, i_d):
        self.group_dict[group].pop(i_d)
        self.group_running_loss[group].pop(i_d)
        self.running_M.pop(i_d)
        self.update_count_k_i()
        self.update_M_stats()
        self.update_group_loss_stats(group)
    
    def add_sample(self, group, i_d, sample_object):
        self.group_dict[group][i_d] = sample_object
        self.group_running_loss[group][i_d] = sample_object.loss
        self.running_M[i_d] = sample_object.measure_M
        self.update_count_k_i()
        self.update_M_stats()
        self.update_group_loss_stats(group)
        
    def __str__(self):
        info = "attribute: " + self.attribute +\
                "\ncount_k_i: " + self.count_k_i +\
                "\ncardinality: " + self.cardinality +\
                "\nsize (current saved samples): " + self.size +\
                "\ngroup_dict: " + dict_to_string(self.group_dict) +\
                "\nmax_M_sample: " + dict_to_string(self.max_M_sample)
        return info
        

def make_sample_object(reservoir, group, batch, asr):
    batch = batch.to(asr.device)
    
    wavs, wav_lens = batch.sig
    wavs, wav_lens = wavs.to(asr.device), wav_lens.to(asr.device)
        
    i_d = batch.id[0]
    duration = batch.duration[0]
    path = batch.wav[0]
    spk_id = batch.spk_id[0]
    wrd = batch.wrd[0]
    age = batch.age[0]
    gender = batch.gender[0]
    accents = batch.accents[0]
    tokens_bos, _ = batch.tokens_bos
    tokens_eos, _ = batch.tokens_eos
    tokens, tokens_lens = batch.tokens
    
    # Forward pass
    feats = asr.modules.wav2vec2(wavs, wav_lens)
    logits = asr.modules.ctc_lin(feats)
    softmax = asr.hparams.softmax(logits) # p_ctc
    
    alpha = asr.hparams.alpha
    beta = asr.hparams.beta
    
    # Evaluate
    loss = asr.hparams.ctc_cost(softmax, tokens, wav_lens, tokens_lens)
    #print(loss)
    #debug = input()
    measure_M = compute_measure_M(reservoir, group, loss, softmax, alpha, beta)

    return Sample(i_d, 
                duration, 
                path, 
                spk_id, 
                wrd, 
                age, 
                gender, 
                accents, 
                batch.sig, 
                tokens_bos, 
                tokens_eos, 
                batch.tokens,
                feats,
                logits,
                softmax,
                loss,
                measure_M)

    
def init_reservoir(reservoir, asr, train_loader):
    
    size = reservoir.size
    attribute = reservoir.attribute
    
    print("\ninit_reservoir\n")
    
    times = 0
    while(True):
        batch = next(train_loader)
        
        if attribute == "age":
            next_group = batch.age[0]
        elif attribute == "gender":
            next_group = batch.gender[0]

        i_d = batch.id[0]
        
        if(next_group != ''):
            sample_object = make_sample_object(reservoir, next_group, batch, asr)
            reservoir.add_sample(next_group, i_d, sample_object)
            times += 1
            #print(str(i+1)+"th append ", batch.id[0])    
        
        if times == size:
            break
        
    print("end of init_reservoir\n")
    
    return train_loader, times

 
def compute_uncertainty(softmax):
    """mean entropy of softmax value"""
    return torch.mean(Categorical(probs = softmax).entropy())
    
def compute_learnability(reservoir, group, loss, alpha, beta):
    #self.group_running_loss = {i:dict() for i in self.groups} # fourties: {i_d : loss}
    #self.group_running_mean_loss = {i:0 for i in self.groups}
    #self.group_running_std_loss = {i:0 for i in self.groups}

    total_group_mean = torch.mean(torch.tensor(list(reservoir.group_running_mean_loss.values())))
    total_group_std = torch.std(torch.tensor(list(reservoir.group_running_mean_loss.values())))
    group_mean = reservoir.group_running_mean_loss[group]
    group_std = reservoir.group_running_std_loss[group]
    
    between_group_confidence = (group_mean - total_group_mean) / total_group_std
    within_group_confidence = (loss - group_mean) / group_std
    
    if torch.isnan(between_group_confidence):
        between_group_confidence = torch.tensor(0.0)
    if torch.isnan(within_group_confidence):
        within_group_confidence = torch.tensor(0.0)
    learnability = alpha * between_group_confidence + beta * within_group_confidence
    
    if reservoir.count_k_i[group] == 0:
        #print("first entering of the group ", group)
        learnability = torch.tensor(0.0)
    
    return learnability
    
        
    
def compute_measure_M(reservoir, group, loss, softmax, alpha, beta):
    uncertainty = compute_uncertainty(softmax)
    learnability = compute_learnability(reservoir, group, loss, alpha, beta)
    measure_M = uncertainty + learnability
    return measure_M
    
def compute_gamma(reservoir, group):
    #self.count_k_i = {i:0 for i in self.groups}
    
    #mean_k = torch.mean(reservoir.count_k_i.values())
    balanced_k = reservoir.size / len(reservoir.count_k_i)
    #print(torch.tensor([float(x) for x in list(reservoir.count_k_i.values())]))
    std_k = torch.std(torch.tensor([float(x) for x in list(reservoir.count_k_i.values())]))
    group_k = reservoir.count_k_i[group]
    
    #prob = norm.pdf(x=group_k, loc=mean_k, scale=std_k)
    prob = norm.pdf(x=group_k, loc=balanced_k, scale=std_k)
    return prob * math.copysign(1, group_k - balanced_k)

#def reservoir_sampling():
    
    
    
    
def info_theory_based_data_selection(asr, size, attribute, train_loader, csv_file):
    """
    size: Reservoir size
    attribute: age / gender
    
    """
    
    alpha = asr.hparams.alpha
    beta = asr.hparams.beta
    
    reservoir = Reservoir(size, attribute)
    train_loader = iter(train_loader)
    
    train_loader , times = init_reservoir(reservoir, asr, train_loader)
    
    next_batch = next(train_loader)
    

    #times = 0
    while next_batch is not None:
        
        if attribute == "age":
            next_group = next_batch.age[0]
        elif attribute == "gender":
            next_group = next_batch.gender[0]
            
        if(next_group != ''):
                        
            next_object = make_sample_object(reservoir, next_group, next_batch, asr)
            
            measure_M = next_object.measure_M
            
            gamma = compute_gamma(reservoir, next_group)
            
            threshold = reservoir.running_mean_M + reservoir.running_std_M * gamma
            

            if measure_M > threshold:
                prob = norm.cdf(x=measure_M.detach().cpu().numpy(), 
                                loc=reservoir.running_mean_M.detach().cpu().numpy(), 
                                scale=reservoir.running_std_M.detach().cpu().numpy())
                
                if np.random.binomial(n=1, p=prob):
                    # replace samples
                    reservoir.delete_sample_with_least_M()
                    reservoir.add_sample(next_group, next_object.id, next_object)
                    
        try:
            next_batch = next(train_loader)    
        except StopIteration:
            print("reached the end of the dataloader")
            break
        times += 1
            
        if(times % 1000 == 0) or times < 30:
            #print(reservoir.group_dict)
            print("\n")
            print(reservoir.count_k_i)
            print("\n\n")
            print("running_mean_M")
            print(reservoir.running_mean_M)
            print("running_std_M")
            print(reservoir.running_std_M)
            
            print("threshold : ", str(threshold))
            print("measure M : ", str(measure_M))
            print("gamma : ", str(gamma))
            print("\n\n\n\n")
        
        
        if(times % 5000 == 0):
            dir_name, base_name = os.path.split(csv_file)
            without_ext, ext = base_name.split(".")
            
            csv_file_ = dir_name + "/" + without_ext + "_" + str(times) + "." + ext
            create_csv(csv_file_, reservoir)
            
    
    # Save the samples in the final reservoir in the csv file
    dir_name, base_name = os.path.split(csv_file)
    without_ext, ext = base_name.split(".")
    
    csv_file_ = dir_name + "/" + without_ext + "_FINAL." + ext
    create_csv(csv_file_, reservoir)
    
    
    print("Sample selection done.\n\nFinal Group Dictionary")
    print(reservoir.group_dict)

    
    return reservoir
    



if __name__ == "__main__":
    
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
        
    train_loader = asr_brain.make_dataloader(train_data, 
                                             stage=sb.Stage.TRAIN, 
                                             **hparams["dataloader_options"])
    
    
    
    size = 100
    attribute = "age"
    #alpha, beta = 0.03, 0.03
    csv_file = hparams["selected_sample_csv"]
    
    
    info_theory_based_data_selection(asr_brain, size, attribute, train_loader, csv_file)
    
    
    print("end of main")
    
    

    
    
