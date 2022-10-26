import argparse
import json
import os
import shutil
import collections
import time
from google.cloud import storage
from pathlib import Path
from typing import Tuple, Union
from contextlib import nullcontext

import torch
import pandas as pd
import numpy as np
from scipy.stats import hmean
from transformers import AutoModelForCausalLM, AutoTokenizer
# from transformers import GPTNeoXForCausalLM, GPTNeoXTokenizerFast

from baselines.efk import EFKHyperParams, EfkRewriteExecutor
from baselines.ft import FTHyperParams, apply_ft_to_model
from baselines.kn import KNHyperParams, apply_kn_to_model
from baselines.mend import MENDHyperParams, MendRewriteExecutor
from dsets import (
    AttributeSnippets,
    CounterFactDataset,
    MENDQADataset,
    get_tfidf_vectorizer,
)
from experiments.causal_trace import ModelAndTokenizer, score_from_batch, predict_token
from experiments.causal_trace import layername, corrupted_forward_pass, find_token_range, make_inputs, simple_make_inputs
from experiments.py.eval_utils_counterfact import compute_rewrite_quality_counterfact
from experiments.py.eval_utils_zsre import compute_rewrite_quality_zsre
from rome import ROMEHyperParams, apply_rome_to_model
from util import nethook
from util.fewshot_utils import predict_model, fewshot_accuracy_sum
from util.generate import generate_fast
from util.globals import *

ALG_DICT = {
    "ROME": (ROMEHyperParams, apply_rome_to_model),
    "FT": (FTHyperParams, apply_ft_to_model),
    "KN": (KNHyperParams, apply_kn_to_model),
    "MEND": (MENDHyperParams, MendRewriteExecutor().apply_to_model),
    "KE": (EFKHyperParams, EfkRewriteExecutor().apply_to_model),
}

DS_DICT = {
    "cf": (CounterFactDataset, compute_rewrite_quality_counterfact),
    "zsre": (MENDQADataset, compute_rewrite_quality_zsre),
}

CODE_DIR='/home/peterhase/belief-loc'
BASE_DIR='/home/peterhase'

def get_override_hparams(args, window_size, central_layer, alg_name):
  # embeddings are being FTed
  if central_layer == -1:
      assert alg_name == 'FT'
      return_dict = {
          'lr': 1e-3,
          'num_steps': 100,
          'norm_constraint': .01,
          'layers': [-1],
      }
      if window_size > 1:
          print("IGNORING WINDOW SIZE FOR TUNING EMBEDDINGS")
  # window size 1 approach
  elif window_size == 1:
    return_dict = {'layers' : [central_layer]}
    if alg_name == "FT":
      return_dict['norm_constraint'] = 1e-4
  # hack for applying ROME to multiple 3 layers
  elif window_size == 3 and alg_name == 'ROME':
    # budget the window size so that we're never editing fewer than three layers
    # hardcoding for now
    layers = [central_layer-1, central_layer, central_layer+1]
    if min(layers) < 0:
        offset = min(layers)
        layers = [layer - offset for layer in layers]
    if max(layers) > max(central_layers):
        offset = max(layers) - num_layers
        layers = [layer - offset for layer in layers]
    return_dict = {
        'layers' : layers,
        'v_num_grad_steps': 4,
        'v_lr': 0.1
        }
  elif window_size > 1:
    layer = central_layer
    window = window_size
    # same layers logic as used in causal tracing. there is clipping at the edges of the network
    layers = list(range(
        max(0, layer - window // 2), min(num_layers, layer - (-window // 2))
        ))
    return_dict = {'layers' : layers}
    if alg_name == "FT":
      return_dict['norm_constraint'] = 2e-4
  # increase number of steps if noising the subject
  if args.fact_forcing:
    if alg_name == "FT":
        return_dict['num_steps'] = 50
    if alg_name == "ROME":
        return_dict['v_num_grad_steps'] = 50
  return return_dict

def sweep_experiment_name(args, model_name, alg_name, ds_name, sweep_params):
  exp_name = f'{model_name}_{alg_name}_outputs_{ds_name}_editing_sweep'  
  for k,v in sweep_params.items():
    _v = str(v).replace(", ", "-")
    if _v == "-1":
        _v = "embeds"
    if _v == "-2":
        _v = "all"
    exp_name += f"_{k[:5]}-{_v}"
  if args.tracing_reversal:
    obj = '_trace-reverse'
  elif args.fact_forcing:
    obj = '_fact-forcing'
  elif args.fact_erasure:
    obj = '_fact-erasure'
  elif args.weight_based_tracing:
    obj = '_weight-tracing'
  return f'{exp_name}{obj}_n{args.dataset_size_limit}.csv'

def ROME_experiment_name(args, model_name, alg_name, ds_name, hparams_to_add):
  exp_name = f'{model_name}/{alg_name}_outputs_{ds_name}'
  if args.tracing_reversal:
    hparams_to_add['trace-reverse'] = 'T'
  if args.fact_forcing:
    hparams_to_add['fact-forcing'] = 'T'
  if args.fact_erasure:
    hparams_to_add['erase'] = 'T'
  if args.weight_based_tracing:
    hparams_to_add['weight-based'] = 'T'
  for k,v in hparams_to_add.items():
    _v = str(v).replace(", ", "-")
    if _v == "-1":
        _v = "embeds"
    exp_name += f"_{k[:5]}-{_v}"
  return exp_name

def ROME_experiment_name_from_override_params(args, model_name, alg_name, ds_name, override_hparams, hparams_class):
  _model_name = model_name.replace('/', '_')
  params_path = os.path.join(f'{CODE_DIR}/hparams/', alg_name, f"{_model_name}.json")
  if alg_name == 'FT':
    params_path = params_path.replace('.json', '_constr.json')
  hparams = hparams_class.from_json(params_path)
  if override_hparams is not None:
      hparams.__dict__.update(override_hparams)
  important_hparam_names = override_hparams.keys() if override_hparams is not None else ['layers']
  important_hparams = {k:v for k,v in hparams.__dict__.items() if any([k==name for name in important_hparam_names])}
  exp_name = ROME_experiment_name(args,
                                  model_name.split('/')[-1],
                                  alg_name,
                                  ds_name,
                                  important_hparams)
  return exp_name

def make_editing_results_df(exp_name, n=1000):
  run_dir = os.path.join(f'{BASE_DIR}/results/', exp_name)
  dataframes = []
  for case_id in range(n):
    case_result_path = os.path.join(run_dir, f"case_{case_id}.json")
    if not os.path.exists(case_result_path):
      print("skipping ", case_result_path, " does not exist")
      continue
    with open(case_result_path, 'r') as f:
      record = json.load(f)
    rewrite_data = record['requested_rewrite']
    prompt = rewrite_data['prompt'].format(rewrite_data['subject'])
    target = rewrite_data['target_true']['str']
    record_dict = {
        'case_id': [record['case_id']],
        'prompt': [prompt],
        'target': [target],
        'subject' : [rewrite_data['subject']],
        'request' : [rewrite_data['target_new']['str']],
        'request_baseline': [rewrite_data['request_baseline']]
    }
    cur_sum = collections.defaultdict(lambda: [])
    data = record
    # compute ROME metrics
    for prefix in ["pre", "post"]:
        # record essence_drift metric
        if 'essence_score' in data[prefix]:
            cur_sum[f"{prefix}_essence_ppl"] = data[prefix]['essence_score']
        # Probability metrics for which new should be lower (better) than true
        for key in ["rewrite_prompts_probs", "paraphrase_prompts_probs"]:
            if prefix not in data or key not in data[prefix]:
                continue
            sum_key_discrete = f"{prefix}_{key.split('_')[0]}_success"
            sum_key_cont = f"{prefix}_{key.split('_')[0]}_diff"
            cur_sum[sum_key_discrete].append(
                np.mean(
                    [
                        x["request_baseline"] > x["target_new"]
                        for x in data[prefix][key]
                    ]
                )
            )
            cur_sum[sum_key_cont].append(
                np.mean(
                    [
                        np.exp(-x["target_new"]) - np.exp(-x["request_baseline"])
                        for x in data[prefix][key]
                    ]
                )
            )
        # Probability metrics for which true should be lower (better) than new
        sum_key_discrete = f"{prefix}_neighborhood_success"
        sum_key_cont = f"{prefix}_neighborhood_diff"
        key = "neighborhood_prompts_probs"
        if prefix in data and key in data[prefix]:
            cur_sum[sum_key_discrete].append(
                np.mean(
                    [
                        x["request_baseline"] < x["target_new"]
                        for x in data[prefix][key]
                    ]
                )
            )
            cur_sum[sum_key_cont].append(
                np.mean(
                    [
                        np.exp(-x["request_baseline"]) - np.exp(-x["target_new"])
                        for x in data[prefix][key]
                    ]
                )
            )
        # zsRE evaluation metrics
        for key in ["rewrite", "paraphrase", "neighborhood"]:
            sum_key = f"{prefix}_{key}_acc"
            key = f"{key}_prompts_correct"
            if prefix not in data or key not in data[prefix]:
                continue
            cur_sum[sum_key].append(np.mean(data[prefix][key]))
        # get harmonic mean averages per point
        for prefix in ["pre", "post"]:
            for k_efficacy, k_generalization, k_specificity in [(
                    f"{prefix}_rewrite_success",
                    f"{prefix}_paraphrase_success",
                    f"{prefix}_neighborhood_success",
                ),
                (
                    f"{prefix}_rewrite_acc",
                    f"{prefix}_paraphrase_acc",
                    f"{prefix}_neighborhood_acc",
                )]:
                  if k_generalization in cur_sum and k_specificity in cur_sum:
                      cur_sum[f"{prefix}_score"] = hmean([
                                  cur_sum[k_efficacy][0],
                                  cur_sum[k_generalization][0],
                                  cur_sum[k_specificity][0]]
                      )
    # add post-pre ppl scores
    if 'essence_score' in data['post']:
        cur_sum['essence_ppl_diff'] = cur_sum['post_essence_ppl'] - cur_sum['pre_essence_ppl'] # lower is better
    # add ROME metrics to record_dict and append to dataframes
    record_dict.update(cur_sum)
    df = pd.DataFrame(record_dict)
    dataframes.append(df)
  if len(dataframes) > 0:
    return_df = pd.concat(dataframes)
  else:
    return_df = pd.DataFrame()
  return return_df

def main(
    args,
    alg_name: str,
    model_name: Union[str, Tuple],
    ds_name: str,
    dataset_size_limit: int,
    do_essence_tests: bool,
    skip_generation_tests: bool,
    conserve_memory: bool,
    mt=None,
    verbose=False,
    override_hparams=None,
    overwrite=False,
    correctness_check=False,
    target_prob_check=0,
):
    # Set algorithm-specific variables
    params_class, apply_algo = ALG_DICT[alg_name]

    # Get run hyperparameters
    _model_name = model_name.replace('/', '_')
    params_path = os.path.join(f'{CODE_DIR}/hparams/', alg_name, f"{_model_name}.json")
    if alg_name == 'FT':
      params_path = params_path.replace('.json', '_constr.json')
    hparams = params_class.from_json(params_path)
    args.hparams = hparams
    if override_hparams is not None:
      hparams.__dict__.update(override_hparams)
    print(f"Executing {alg_name} with parameters {hparams}")

    # Determine run directory
    important_hparam_names = override_hparams.keys() if override_hparams is not None else ['layers']
    important_hparams = {k:v for k,v in hparams.__dict__.items() if any([k==name for name in important_hparam_names])}
    exp_name = ROME_experiment_name(args,
                                    model_name.split('/')[-1],
                                    alg_name,
                                    ds_name,
                                    important_hparams)
    run_dir = f'{BASE_DIR}/results/{exp_name}'
    os.makedirs(run_dir, exist_ok=True)
    print(f"Results will be stored at {run_dir}")
    # copy hparams to results dir
    copy_to_path =  os.path.join(run_dir, 'hparams.json')
    if not os.path.exists(copy_to_path):
        shutil.copyfile(params_path, copy_to_path)
    
    # Instantiate vanilla model
    if mt is None:
      print("Instantiating model")
      model = AutoModelForCausalLM.from_pretrained(model_name).cuda()
      tok = AutoTokenizer.from_pretrained(model_name)
      tok.pad_token = tok.eos_token
    else:
      model, tok = mt.model, mt.tokenizer
      tok.pad_token = tok.eos_token

    # Load data
    print("Loading dataset, attribute snippets, tf-idf data")
    snips = AttributeSnippets(DATA_DIR)
    vec = get_tfidf_vectorizer(DATA_DIR) if not skip_generation_tests else None

    ds_class, ds_eval_method = DS_DICT[ds_name]
    ds = ds_class(DATA_DIR, size=dataset_size_limit, tok=tok)
    # Iterate through dataset
    for record in ds:
        case_id = record["case_id"] if 'case_id' in record else 'known_id'
        case_result_path = os.path.join(run_dir, f"case_{case_id}.json")
        rewrite_this_point = overwrite or not os.path.exists(case_result_path)
        if rewrite_this_point:
            print("Starting point: ", case_id)
            # print info for this point
            request = record["requested_rewrite"]
            subject = record["requested_rewrite"]['subject']
            prompt = request['prompt'].format(subject)
            request['full_prompt'] = prompt
            target_true = request['target_true']['str']
            paraphrase_prompts = record["paraphrase_prompts"]
            neighborhood_prompts = record["neighborhood_prompts"]
            if verbose:
                print("Updating point:"
                    f" orig update: [{prompt}] -> [{request['target_new']['str']}]"
                    f"\n True label: {target_true}"
                    f"\n Paraphrases: {paraphrase_prompts[:2]}"
                    f"\n Neighbors: {neighborhood_prompts[:2]}")

            # check if we should skip based on correctness or probability checks
            if correctness_check or target_prob_check > 0:
                import pdb; pdb.set_trace()
                eval_this_point = 0
                if correctness_check:
                    gen_batch = simple_make_inputs(tok, prompts=[prompt])
                    samples, scores, _ = predict_model(mt, 
                                            [prompt], 
                                            answers=None, 
                                            trigger_phrase=None, 
                                            max_decode_steps=48)
                    is_correct = fewshot_accuracy_sum(samples, [target_true])
                    eval_this_point += is_correct
                if target_prob_check > 0:
                    preds, scores, _ = predict_model(mt, [prompt], answers=[target_true])
                    meets_target_prob = scores[0].item() > target_prob_check
                    eval_this_point += meets_target_prob
                if not eval_this_point > 0:
                    if verbose:
                        print(" Skipping this point due to it being incorrect and not meeting the minimum target prob.")
                        if target_prob_check > 0: print(f" Target prob: {scores}")
                        if correctness_check:     print(f" Pred: {preds}")
                    continue

            # generate essence_texts for evaluation if needed
            if do_essence_tests or not skip_generation_tests:
                essence_prompt = "{} is a".format(subject)
                # second condition here checks that subject appears verbatim in first 200 characters of essence text, which is necessary later on
                # essence_texts = snips.names_to_samples[subject]
                # if subject == 'Inner Circle railway line':
                    # print("debug the condition below for Inner Circle railway line")
                    # import pdb; pdb.set_trace()
                # if len(essence_texts) == 0 or not all([subject in essence_text[:200] for essence_text in essence_texts]):
                if verbose:
                    print("GENERATING ESSENCE TEXTS")
                essence_texts = generate_fast(
                    model,
                    tok,
                    [essence_prompt],
                    n_gen_per_prompt=5,
                    max_out_len=100,
                )
                snips.names_to_samples[subject] = essence_texts
                # snips.names_to_samples[subject].extend(essence_texts)
                # elif verbose:
                    # print("using wikipedia essence texts")
                if verbose:
                    for text in snips.names_to_samples[request['subject']][:2]:
                        print(f" Essence text: {text[:200]}")

            # adjust targets and define 'request_baseline' based on objectives. note model does not necesarily predict 'request_baseline' value before rewriting
            num_noise_samples = 10
            e_range = find_token_range(tok, substring=subject, prompt_str=prompt)
            prior_prob = None
            if args.tracing_reversal:
                gen_batch = simple_make_inputs(tok, prompts=[prompt] * (num_noise_samples))
                _, noised_pred_id = corrupted_forward_pass(mt.model, None, gen_batch, tokens_to_mix=e_range, noise=hparams.editing_noise)
                noised_pred_token = tok.decode([noised_pred_id])
                request['request_baseline'] = request['target_true']['str']
                request['target_new']['str'] = noised_pred_token
                request['target_new']['id'] = 'noised-input'
                if verbose:
                    score_batch = make_inputs(tok, [prompt], targets=[noised_pred_token])
                    init_target_prob = score_from_batch(model, score_batch)
                    print(f" NEW TARGET PREDICTION: \"{noised_pred_token}\" with init pred prob: {init_target_prob.item():.4f}")
            elif args.fact_erasure:
                batch = make_inputs(mt.tokenizer, prompts=[prompt] * num_noise_samples, targets=[target_true] * num_noise_samples)
                prior_prob = corrupted_forward_pass(mt.model, batch, None, tokens_to_mix=e_range, noise=hparams.editing_noise)
                if verbose:
                    print(f" TARGET/PRIOR PROB: {prior_prob.item():.4f}")
                request['request_baseline'] = mt.tokenizer.eos_token_id # arbitrary token, won't use these metrics anyway
                request['target_new'] = request['target_true']
            elif args.fact_forcing or args.weight_based_tracing:
                gen_batch = simple_make_inputs(tok, prompts=[prompt] * (num_noise_samples))
                _, noised_pred_id = corrupted_forward_pass(mt.model, None, gen_batch, tokens_to_mix=e_range, noise=hparams.editing_noise)
                noised_pred_token = tok.decode([noised_pred_id])
                request['request_baseline'] = noised_pred_token
                request['target_new'] = request['target_true']
            else:
                request['request_baseline'] = request['target_true']['str']
            if verbose:
                print(" request baseline: ", request['request_baseline'])
                
            # get additional functions and variables based on objectives
            # make noise_embeddings function for nethook contextmanager if noising the subject
            if args.fact_forcing or args.weight_based_tracing:
                # define noise embeddings function
                prng = np.random.RandomState(1) 
                embed_layername = layername(model, 0, 'embed')
                # define function that noises embeddings at tokens_to_mix indices
                def noise_embeddings(x, layer):
                    # skip noising if seq is a single token (must be bos/eos for open-ended generation)
                    if (x.shape[1] == 1):
                        return x
                    if layer == embed_layername:
                        # If requested, we corrupt a range of token embeddings on batch items x[1:]
                        if e_range is not None:
                            b, e = e_range
                            embeds_noise = torch.from_numpy(prng.randn(x.shape[0], e - b, x.shape[2])).to(x.device)
                            x[:, b:e] += hparams.editing_noise * embeds_noise
                        # print("added noise to embeds: ", embeds_noise)
                        return x
                    else:
                        return x

            # Compute weight changes + record weights that changed
            start = time.time()
            args_conserve_memory = (
                dict(return_orig_weights_device=("cpu" if conserve_memory else "cuda"))
                if conserve_memory
                else dict()
            )
            with torch.enable_grad(), nethook.TraceDict(model, [embed_layername], edit_output=noise_embeddings) if args.fact_forcing else nullcontext() as td:
              edited_model, weights_copy = apply_algo(
                  args,
                  model,
                  tok,
                  [request],
                  hparams,
                  copy=False,
                  return_orig_weights=True,
                  num_noise_samples=num_noise_samples,
                  prior_prob=prior_prob,
                  **args_conserve_memory,
              )
            exec_time = time.time() - start
            print("Execution took", exec_time)

            # Execute evaluation suite
            start = time.time()
            with torch.no_grad(): #, nethook.TraceDict(model, [embed_layername], edit_output=noise_embeddings) if args.fact_forcing else nullcontext() as td:
                metrics = {
                    "case_id": case_id,
                    "requested_rewrite": request,
                    "time": exec_time,
                    "post": ds_eval_method(args, edited_model, tok, record, snips, vec, skip_generation_tests),
                }

                for k, v in weights_copy.items():
                    nethook.get_parameter(model, k)[...] = v.to("cuda")
                metrics["pre"] = ds_eval_method(args, model, tok, record, snips, vec, skip_generation_tests)

            # print("metrics: ", metrics)
            print("Evaluation took", time.time() - start)
            # Dump metrics in .json
            with open(case_result_path, "w") as f:
                json.dump(metrics, f, indent=1)
            print('\n')
        else:
            if verbose:
              print(f"skipping {case_result_path}, already run")
            else:
              pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--alg_name",
        choices=["ROME", "FT", "KN", "MEND", "KE"],
        default="ROME",
        help="Editing algorithm to use. Results are saved in results/<alg_name>/<run_id>, "
        "where a new run_id is generated on each run. "
        "If continuing from previous run, specify the run_id in --continue_from_run.",
        required=True,
    )
    parser.add_argument(
        "--model_name",
        choices=["gpt2-medium", "gpt2-large", "gpt2-xl", "EleutherAI/gpt-j-6B"],
        default="gpt2-xl",
        help="Model to edit.",
        required=True,
    )
    parser.add_argument(
        "--edit_layer",
        type=int,
        default=0,
        help="Layer at which to edit the model weights. Set to -2 to defer to hparam sweep params below",
    )
    parser.add_argument(
        "--ds_name",
        choices=["cf", "zsre"],
        default="cf",
        help="Dataset to perform evaluations on. Either CounterFact (cf) or zsRE (zsre).",
    )
    parser.add_argument(
        "--continue_from_run",
        type=str,
        default=None,
        help="If continuing from previous run, set to run_id. Otherwise, leave as None.",
    )
    parser.add_argument(
        "--window_sizes",
        type=str,
        default='1',
        help="Window sizes separted by spaces to use for editing method",
    )
    parser.add_argument(
        "--dataset_size_limit",
        "-n",
        type=int,
        default=1000,
        help="Truncate CounterFact to first n records.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite previous experiment results",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="More printing during editing",
    )
    parser.add_argument(
        "--do_essence_tests",
        action="store_true",
        help="Do the essence drift generation test regardless of args.skip_generation_tests",
    )
    parser.add_argument(
        "--skip_generation_tests",
        dest="skip_generation_tests",
        action="store_true",
        help="Only run fast probability-based tests without slow generation tests. "
        "Useful for quick debugging and hyperparameter sweeps.",
    )
    parser.add_argument(
        "--tracing_reversal",
        action="store_true",
        help="Rather than changing output from target_true to target_new, change it to the prediction obtained from the noised causal tracing input",
    )
    parser.add_argument(
        "--fact_forcing",
        action="store_true",
        help="Rather than change o-true to o-new for (s,r,.) input, change o-noise to o-true for (s-noise, r,.) input",
    )
    parser.add_argument(
        "--fact_erasure",
        action="store_true",
        help="See paper for description",
    )
    parser.add_argument(
        "--weight_based_tracing",
        action="store_true",
        help="See paper for description",
    )
    parser.add_argument(
        "--conserve_memory",
        dest="conserve_memory",
        action="store_true",
        help="Reduce memory usage during evaluation at the cost of a minor slowdown. "
        "Backs up model weights on CPU instead of GPU.",
    )
    parser.add_argument(
        "--run",
        type=int,
        default=1,
        choices=[0,1],
    )
    parser.set_defaults(skip_generation_tests=True, do_essence_tests=True, conserve_memory=True, verbose=False, overwrite=False,
                        tracing_reversal=False, fact_forcing=False)
    args = parser.parse_args()

    # load model
    if args.run:
        torch.set_grad_enabled(False)
        model_name = args.model_name
        torch_dtype = torch.float16 if '20b' in model_name else None
        mem_usage = True
        print("Loading model...")
        if '20b' not in model_name:
            mt = ModelAndTokenizer(model_name, low_cpu_mem_usage=mem_usage, torch_dtype=torch_dtype)
            torch.cuda.empty_cache()
            mt.model.eval().cuda()
            mt.tokenizer.add_special_tokens({'pad_token' : mt.tokenizer.eos_token})
            # mt.tokenizer.pad_token = mt.tokenizer.eos_token
            # mt.tokenizer.pad_token_id = mt.tokenizer.eos_token_id
        else:
            model = GPTNeoXForCausalLM.from_pretrained("EleutherAI/gpt-neox-20b", 
                                                        device_map={
                                                            'embed_out' : 0,
                                                            'gpt_neox.embed_in' : 0,
                                                            'gpt_neox.layers': 1,
                                                            'gpt_neox.final_layer_norm' : 0,
                                                        },
                                                        low_cpu_mem_usage=mem_usage,
                                                        torch_dtype=torch_dtype)
            torch.cuda.empty_cache()
            model.eval().cuda()
            tokenizer = GPTNeoXTokenizerFast.from_pretrained("EleutherAI/gpt-neox-20b")
            mt = ModelAndTokenizer(model=model, tokenizer=tokenizer, torch_dtype=torch_dtype)
            mt.tokenizer.add_special_tokens({'pad_token' : mt.tokenizer.eos_token})
        # num_layers = mt.num_layers

    # set experiment args
    RUN_EXPERIMENT = args.run # set to false to just collect results
    num_points = args.dataset_size_limit
    alg_name = args.alg_name
    model_name = args.model_name
    assert alg_name in ["FT", "ROME"]
    hparams_class = FTHyperParams if alg_name == "FT" else ROMEHyperParams
    ds_name = args.ds_name
    window_sizes = [int(x) for x in args.window_sizes.split()]
    if 'gpt2' in model_name:
        central_layers = list(range(0, 48, 4)) + [17, 47]
        num_layers = 48
    if '6B' in model_name:
        central_layers = list(range(0, 28, 4)) + [5, 27]
        num_layers = 28
    if alg_name == 'FT' and 1 in window_sizes and not args.fact_forcing:
        central_layers = [-1] + central_layers
    if args.edit_layer > -2:
        central_layers = [args.edit_layer]
    print("Starting sweep with hparams:")
    print("- window_sizes: ", window_sizes)
    print("- central_layers: ", central_layers)

    # main experiment loop
    results_dfs = []
    for window_size in window_sizes:
        for central_layer in central_layers:
            override_hparams = get_override_hparams(args, window_size, central_layer, alg_name)
            if RUN_EXPERIMENT:
                main(
                    args,
                    alg_name=alg_name,
                    model_name=model_name,
                    ds_name=ds_name,
                    dataset_size_limit=num_points,
                    do_essence_tests=args.do_essence_tests,
                    skip_generation_tests=args.skip_generation_tests,
                    conserve_memory=args.conserve_memory,
                    mt=mt,
                    override_hparams=override_hparams,
                    verbose=args.verbose,
                    overwrite=args.overwrite,
                    correctness_check=True,
                    target_prob_check=.1
                )
            # accumulate results
            exp_name = ROME_experiment_name_from_override_params(args, model_name, alg_name, ds_name, override_hparams, hparams_class)
            editing_results_df = make_editing_results_df(exp_name, n=num_points)
            editing_results_df['edit_method'] = alg_name
            editing_results_df['edit_central_layer'] = central_layer
            editing_results_df['edit_window_size'] = window_size
            results_dfs.append(editing_results_df)
    
    # combine and save results
    results_df = pd.concat(results_dfs)
    _model_name = model_name.split('/')[-1]
    sweep_params = {'ws': window_sizes, 'layers': args.edit_layer}
    ovr_exp_name = sweep_experiment_name(args, _model_name, alg_name, ds_name, sweep_params)
    file_name = f'{ovr_exp_name}.csv'
    save_path = f'{BASE_DIR}/results/{file_name}'
    results_df.to_csv(save_path, index=False)
    # upload results csv to google bucket
    storage_client = storage.Client()
    bucket = storage_client.get_bucket('research-brain-belief-localization-xgcp')
    blob = bucket.blob(f'output/{file_name}')
    blob.upload_from_filename(save_path)

    print(f"saving csv at {save_path}...")
    metrics = ['post_rewrite_success', 'post_rewrite_diff', 'post_neighborhood_success', 'post_neighborhood_diff', 'post_paraphrase_success', 'post_paraphrase_diff', 'essence_ppl_diff', 'post_score']
    if len(window_sizes) == 1 and len(central_layers) == 1:
        print("\nfinal metrics: ")
        for metric in metrics:
            if metric in results_df.columns:
                avg_val = np.mean(results_df.loc[:,metric])
                print(f" {metric:.20s}: {avg_val:.3f}")
            else:
                print(f" missing {metric}")
    if args.verbose:
        print("\nsample rows:")
        print(results_df.loc[:,['case_id', 'subject', 'target', 'request', 'post_rewrite_success', 'post_neighborhood_success', 'post_paraphrase_success', 'post_score', 'essence_ppl_diff']])
    


