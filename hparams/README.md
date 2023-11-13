# Train Settings

1. multi-lingual model : "1_train_en+de.yaml"
2. fine-tuning model : first train en model with "2_train_en_first.yaml" and then fine-tune with "3_train_de_second.yaml"
3. naive memory replay model : first train en model with "2_train_en_first.yaml" and then train with "4_train_de_second_with_naive_mem_replay.yaml"

# What you should care / modify
- You should change <!PLACEHOLDER> in regard to your own setting.
- Check output_folder is set right
- You should have the preprocessed train.csv, test.csv, dev.csv in your save_folder before start training in order to avoid data_preprocessing
