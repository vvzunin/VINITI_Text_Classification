import pandas as pd
import numpy as np
import json
import torch
from transformers import BertForSequenceClassification, BertTokenizer
from datasets import Dataset
from peft import PeftConfig, PeftModel
from torch.utils.data import DataLoader
import os
import sys

os.environ["TRANSFORMERS_NO_ADDITIONAL_MODULES"] = "1"

def prepair_model(
  n_classes, lora_model_path, pre_trained_model_name="DeepPavlov/rubert-base-cased"
):
  old_stdout = sys.stdout # backup current stdout
  sys.stdout = open(os.devnull, "w")

  model = BertForSequenceClassification.from_pretrained(
    pre_trained_model_name,
    problem_type="multi_label_classification",
    num_labels=n_classes,
  )
  
  for param in model.parameters():
    param.requires_grad = False
    
  PeftConfig.from_pretrained(lora_model_path)
  model = PeftModel.from_pretrained(model, lora_model_path, torch_dtype=torch.float16)
  sys.stdout = old_stdout 
  return model

def prepair_data_level1(file_path, format="multidoc", encoding="cp1251"):
  if format == "multidoc":
    df_test = pd.read_csv(
      file_path, sep="\t", encoding=encoding, on_bad_lines="error", index_col="id"
    )
    df_test = df_test.fillna("")
    df_test["text"] = (
      df_test["title"].apply(lambda x: x + " [SEP] ") + df_test["body"]
    )
    df_test["text"] = (
      df_test["text"].apply(lambda x: str(x) + " [SEP] ") + df_test["keywords"]
    )
    df_test = df_test.drop(columns=["title", "keywords", "body"])
    df_test = df_test.dropna(subset=["text"], axis=0)
  elif format == "plain":
    text = open(file_path, "r", encoding=encoding).readlines()
    text = "".join(text)
    text = text.replace("\n", " ")
    df_test = pd.DataFrame({"text": text, "correct": "###"}, index=["#"])
    df_test.index.name='id'
  return df_test

def prepair_data_level2(path, model_path, df_test, preds, threshold):
  y_pred_list = []
  for pred in preds:
    a = np.array(pred)
    y_pred_list.append((a > threshold).tolist())
  preds = y_pred_list

  with open(os.path.join(model_path, "dict.json"), "r") as code_file:
    grnti_mapping_dict_true_numbers = json.load(
      code_file
    )  # Загружаем файл с кодами

  with open(os.path.join(path, "dicts", "GRNTI_1_ru.json"), "r", encoding="utf-8") as code_file:
    grnti_mapping_dict_true_names = json.load(code_file)  # Загружаем файл с кодами

  list_GRNTI = []
  for el in preds:
    list_elments = []

    for index, propab in enumerate(el):
      if propab == 1:
        list_elments.append(index)
    list_GRNTI.append(list_elments)

  grnti_mapping_dict_true_numbers_reverse = {
    y: x for x, y in grnti_mapping_dict_true_numbers.items()
  }
  list_true_numbers_GRNTI = []
  for list_el in list_GRNTI:
    list_numbers = []
    for el in list_el:
      list_numbers.append(grnti_mapping_dict_true_numbers_reverse[el])
    list_true_numbers_GRNTI.append(list_numbers)

  list_thems = []
  for list_true in list_true_numbers_GRNTI:
    sring_per_element = ""
    for el in list_true:
      sring_per_element += grnti_mapping_dict_true_names[el] + "; "
    list_thems.append(sring_per_element)
  df_test.loc[:, 'text'] = df_test.loc[:, 'text'].radd(list_thems)
  return df_test

def get_input_ids_attention_masks_token_type(df, tokenizer, max_len):
  # Токенизация
  input_ids = []
  attention_masks = []
  token_type_ids = []
  # Для каждого текста...
  for sent in df["text"]:  # text_with_GRNTI1_names
    encoded_dict = tokenizer.encode_plus(
      sent,
      max_length=max_len,
      return_tensors="pt",
      truncation=True,
      return_token_type_ids=True,
      return_attention_mask=True,
      padding="max_length",
    )
    # Добавляем закодированный текст в list.
    input_ids.append(encoded_dict["input_ids"])
    # Добавляем attention mask (Отделяем padding от non-padding токенов).
    attention_masks.append(encoded_dict["attention_mask"])
    # Добавляем token_type_ids, тк у нас есть [SEP] в тексах
    token_type_ids.append(encoded_dict["token_type_ids"])

  # Переводим листы в тензоры.
  input_ids = torch.cat(input_ids, dim=0)
  attention_masks = torch.cat(attention_masks, dim=0)
  token_type_ids = torch.cat(token_type_ids, dim=0)

  return input_ids, attention_masks, token_type_ids

def collate_fn(batch):
  result = {}
  for el in batch:
    for key in el.keys():
      result.setdefault(key, []).append(el[key])
  for key in result.keys():
    result[key] = torch.tensor(result[key])
  return result

def prepair_dataset(
  df_test,
  workers,
  max_number_tokens=512,
  pre_trained_model_name="DeepPavlov/rubert-base-cased",
):

  tokenizer = BertTokenizer.from_pretrained(
    pre_trained_model_name, do_lower_case=True
  )

  input_ids_test, attention_masks_test, token_type_ids_test = (
    get_input_ids_attention_masks_token_type(
      df_test, tokenizer=tokenizer, max_len=max_number_tokens
    )
  )

  dataset_test = Dataset.from_dict(
    {
      "input_ids": input_ids_test,
      "attention_mask": attention_masks_test,
      "token_type_ids": token_type_ids_test,
    }
  )

  test_dataloader = DataLoader(dataset_test, num_workers=workers, batch_size=8, collate_fn=collate_fn)
  return test_dataloader

def make_predictions(model, dataset_test, device):
  model.eval()
  y_pred_list = []
  model.to(device)

  # count = 0

  for batch in dataset_test:

    inputs = batch["input_ids"].to(device=device, dtype=torch.long)
    mask = batch["attention_mask"].to(device=device)

    with torch.no_grad():
      output = model(input_ids=inputs, attention_mask=mask)

    # Move logits and labels to CPU
    logits = output.logits.detach().cpu()

    logits_flatten = (torch.sigmoid(logits).numpy()).tolist()

    y_pred_list.extend(logits_flatten)

  return y_pred_list

def toRubrics(model_path, preds, threshold = 0.5):
  with open(os.path.join(model_path, "dict.json"), "r") as code_file:
    grnti_mapping_dict_true_numbers = json.load(
      code_file
    )  # Загружаем файл с кодами

  list_GRNTI = []
  for el in preds:
    list_elments = {}

    for index, propab in enumerate(el):
      if propab >= threshold:
        list_elments[index] = propab
    list_GRNTI.append(list_elments)

  grnti_mapping_dict_true_numbers_reverse = {
    y: x for x, y in grnti_mapping_dict_true_numbers.items()
  }
  list_true_numbers_GRNTI = []
  for list_el in list_GRNTI:
    list_numbers = {}
    for el in list_el:
      list_numbers[grnti_mapping_dict_true_numbers_reverse[el]] = list_el[el]
    list_true_numbers_GRNTI.append(list_numbers)
  return list_true_numbers_GRNTI

def save_rubrics(dataset, list_true_numbers_GRNTI, args, prog, header = False, encoding="cp1251"):
  df = pd.DataFrame(columns=['result', 'rubricator', 'language', 'threshold', 'version', 'normalize', 'correct'])
  df.index.name='id'
  indexes = dataset.index.tolist()
  for i in range(len(list_true_numbers_GRNTI)):
    res = ''
    if (len(list_true_numbers_GRNTI[i]) == 0):
      res = 'EMPTY'
    else:
      k = []
      list_true_numbers_GRNTI[i] = dict(sorted(list_true_numbers_GRNTI[i].items(), key=lambda item: item[1], reverse=True))
      for key, value in list_true_numbers_GRNTI[i].items():
        k.append('{}-{:1.5f}'.format(key, value))
      res = '\\'.join(k)
    df.loc[indexes[i]] = [res, args['level'], args['language'], args['threshold'], prog['version'], args['normalisation'], dataset.iloc[i]['correct']]
  df.to_csv(args['output_file'], sep='\t', mode='a', header=header, encoding=encoding)