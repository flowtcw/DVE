"""Parse logs from experiments in deprecated format and summarise in new format.

There are two types of "old" logs:
1. v1 of the code directly used tensorboard event files, rather than text files, so to
   extract logs from these experiments we need to parse the tensorboard event files.
2. v2 of the code switched to using text logs, but used a slightly format (did not print
   the config at the start, and in some cases overran by a few epochs).

Both logs styles are converted to the current logging format, and the last checkpoint
is re-evaluated.

NOTE: This module requires tensorflow to parse the tensorboard files.
"""
import os
import time
import json
import argparse
import logging
import shutil
import numpy as np
from pathlib import Path
from test_matching import evaluation
from logger import setup_logging
from parse_config import ConfigParser


def parse_tboard_files(rel_dir):
    import tensorflow as tf
    from protobuf_to_dict import protobuf_to_dict
    tboard_files = list(Path(rel_dir).glob("events.out.tfevents.*"))
    assert len(tboard_files) == 1, "expected a single tensorboard file"
    tboard_file_path = tboard_files[0]
    gen_log = [f"This log was generated from tensorboard file {tboard_file_path.name}"]
    count = 0
    try:
        for summary in tf.train.summary_iterator(str(tboard_file_path)):
            count += 1
            summary = protobuf_to_dict(summary)
            if count > 1000:
                break
            if "step" not in summary:
                continue
            step = summary["step"]
            ts = time.strftime('%Y-%m-%d:%Hh%Mm%Ss', time.gmtime(summary["wall_time"]))
            if "summary" in summary and summary["summary"]["value"]:
                value = summary["summary"]["value"]
                if "simple_value" in value[0]:
                    vals = [f"{x['tag']}: {x['simple_value']}" for x in value]
                    if step % 2000 == 0 or value[0]["tag"] != "train/loss":
                        row = f"{ts} step: {step}, {','.join(vals)}"
                        gen_log.append(row)
                        print(row)
                elif "image" in value[0]:
                    pass
                else:
                    import ipdb; ipdb.set_trace()
    except tf.errors.DataLossError as DLE:
        print(f"{DLE} Could not parse any further information")
    print(f"parsed {count} summaries")
    return gen_log


def modernize_exp_dir(experiments, checkpoints, save_dir, refresh):
    for key in experiments:
        rel_dir = checkpoints[key]["timestamp"]

        timestamp = rel_dir.split("/")[-1]
        src_config = Path(rel_dir) / "config.json"
        src_model = Path(rel_dir) / "model_best.pth"
        dest_log = Path(save_dir) / "log" / key / timestamp / "info.log"
        config_path = Path(save_dir) / "models" / key / timestamp / "config.json"
        model_path = Path(save_dir) / "models" / key / timestamp / "model_best.pth"

        config_path.parent.mkdir(exist_ok=True, parents=True)
        if not config_path.exists() or refresh:
            print(f"copying config: {str(src_config)} -> {str(config_path)}")
            shutil.copyfile(str(src_config), str(config_path))
        else:
            print(f"transferred config found at {str(config_path)}, skipping...")


        if not model_path.exists() or refresh:
            print(f"copying model: {str(src_model)} -> {str(model_path)}")
            shutil.copyfile(str(src_model), str(model_path))
        else:
            print(f"transferred model found at {str(model_path)}, skipping...")

        if not dest_log.exists() or refresh:
            generated_log = parse_tboard_files(rel_dir)
            dest_log.parent.mkdir(exist_ok=True, parents=True)
            setup_logging(save_dir=dest_log.parent)
            logger = logging.getLogger("tboard-parser")
            for row in generated_log:
                logger.info(row)

            # re-run pixel matching evaluation
            best_ckpt_path = Path(rel_dir) / "model_best.pth"
            eval_args = argparse.ArgumentParser()
            eval_args.add_argument("--config", default=str(config_path))
            eval_args.add_argument("--device", default="0")
            eval_args.add_argument("--mini_eval", default=1)
            eval_args.add_argument("--resume", default=best_ckpt_path)
            eval_config = ConfigParser(eval_args, slave_mode=True)
            evaluation(eval_config, logger=logger)
        else:
            print(f"generated log found at {str(dest_log)}, skipping...")


def parse_old_log(log_path, config_path, fixed_epochs):
    """A few the experiments were launched without the correct stopping criteria, so
    we fix all models to use the same checkpoint and remove the excesss log. For
    reference the 'excess' consists of 5 or 6 epochs of training after the planned 100.
    The checkpoints generated by these additional checkpoints are discarded, rather than
    evaluated."""
    with open(config_path, "r") as f:
        config = f.read().splitlines()
    with open(log_path, "r") as f:
        log = f.read().splitlines()
    tag = f"checkpoint-epoch{fixed_epochs}.pth"
    presence = [(tag in row and "trainer" in row) for row in log]
    assert sum(presence) == 1, "expected single occurence of log tag"
    pos = np.where(presence)[0].item()
    timestamp = Path(log_path).parent.stem
    gen_log = [f"This log was generated from an existing log for experiemnt {timestamp}"]
    gen_log += ["Launching experiment with config:"]
    offset = "Training took" in log[pos + 1]
    return gen_log + config + log[:pos + 1 + offset]


def standardize_exp_dir(experiments, save_dir, checkpoints, refresh):
    """Restructure logs in canonical format (deals with older versions that were
    run different config setups).
    """
    for key in experiments:
        timestamp = checkpoints[key]["timestamp"]
        epoch = checkpoints[key]["epoch"]

        log_path = Path(save_dir) / "log" / key / timestamp / "info.log"
        config_path = Path(save_dir) / "models" / key / timestamp / "config.json"
        ckpt_name = f"checkpoint-epoch{epoch}.pth"
        model_path = Path(save_dir) / "models" / key / timestamp / ckpt_name
        assert log_path.exists(), "log was not found"
        assert config_path.exists(), "config was not found"
        assert model_path.exists(), "model was not found"

        # make a backup to preserve the original
        backup_log = f"{str(log_path)}.backup"

        if not Path(backup_log).exists() or refresh:
            shutil.copyfile(str(log_path), backup_log)
            generated_log = parse_old_log(backup_log, config_path, epoch)
            log_path.unlink()
            setup_logging(save_dir=log_path.parent)
            logger = logging.getLogger("log-gen")
            for row in generated_log:
                logger.info(row)

            # re-run pixel matching evaluation (this was missing in the old format)
            eval_args = argparse.ArgumentParser()
            eval_args.add_argument("--config", default=str(config_path))
            eval_args.add_argument("--device", default="3")
            eval_args.add_argument("--mini_eval", default=1)
            eval_args.add_argument("--resume", default=model_path)
            eval_config = ConfigParser(eval_args, slave_mode=True)
            evaluation(eval_config, logger=logger)
        else:
            print(f"backup log found at {str(backup_log)}, skipping...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="modernize")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--save_dir", default="data/saved")
    parser.add_argument("--device", default="")
    parser.add_argument("--dep_exps", default="misc/experiments-deprecated.json")
    parser.add_argument("--non_std_exps", default="misc/experiments-non-standard.json")
    parser.add_argument("--ckpts_path", default="misc/server-checkpoints.json")
    args = parser.parse_args()

    if args.device:
        os.environ["CUDA_VISIBLE_DEVICES"] = args.device

    with open(args.ckpts_path, "r") as f:
        ckpts = json.load(f)

    if args.task == "modernize":
        with open(args.dep_exps, "r") as f:
            dep_experiments = json.load(f)
        modernize_exp_dir(
            refresh=args.refresh,
            checkpoints=ckpts,
            save_dir=args.save_dir,
            experiments=dep_experiments,
        )
    elif args.task == "standardize":
        with open(args.non_std_exps, "r") as f:
            non_std_experiments = json.load(f)
        standardize_exp_dir(
            refresh=args.refresh,
            checkpoints=ckpts,
            save_dir=args.save_dir,
            experiments=non_std_experiments,
        )