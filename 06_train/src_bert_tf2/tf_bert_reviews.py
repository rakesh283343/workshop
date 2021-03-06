import time
import random
import pandas as pd
from glob import glob
import argparse
import json
import subprocess
import sys
import os
import tensorflow as tf
#subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'tensorflow==2.0.0'])
subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'transformers'])
subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'sagemaker-tensorflow==2.0.0.1.1.0'])
from transformers import DistilBertTokenizer
from transformers import TFDistilBertForSequenceClassification
from transformers import TextClassificationPipeline
from transformers.configuration_distilbert import DistilBertConfig

MAX_SEQ_LENGTH = 128
BATCH_SIZE = 128 
EVAL_BATCH_SIZE = BATCH_SIZE * 2
EPOCHS = 10 
STEPS_PER_EPOCH = 1000
VALIDATION_STEPS = 1000
CLASSES = [1, 2, 3, 4, 5]
# XLA is an optimization compiler for tensorflow
USE_XLA = False 
# Mixed precision can help to speed up training time
USE_AMP = False


def select_data_and_label_from_record(record):
    x = {
        'input_ids': record['input_ids'],
        'input_mask': record['input_mask'],
        'segment_ids': record['segment_ids']
    }

    y = record['label_ids']
    print(y)

    return (x, y)


def file_based_input_dataset_builder(channel,
                                     input_filenames,
                                     pipe_mode,
                                     is_training,
                                     drop_remainder):

    # For training, we want a lot of parallel reading and shuffling.
    # For eval, we want no shuffling and parallel reading doesn't matter.

    if pipe_mode:
        print('***** Using pipe_mode with channel {}'.format(channel))
        from sagemaker_tensorflow import PipeModeDataset
        dataset = PipeModeDataset(channel=channel,
                                  record_format='TFRecord')
    else:
        print('***** Using input_filenames {}'.format(input_filenames))
        dataset = tf.data.TFRecordDataset(input_filenames)

    dataset = dataset.repeat(EPOCHS * STEPS_PER_EPOCH)
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    name_to_features = {
      "input_ids": tf.io.FixedLenFeature([MAX_SEQ_LENGTH], tf.int64),
      "input_mask": tf.io.FixedLenFeature([MAX_SEQ_LENGTH], tf.int64),
      "segment_ids": tf.io.FixedLenFeature([MAX_SEQ_LENGTH], tf.int64),
      "label_ids": tf.io.FixedLenFeature([], tf.int64),
      "is_real_example": tf.io.FixedLenFeature([], tf.int64),
    }

    def _decode_record(record, name_to_features):
        """Decodes a record to a TensorFlow example."""
        example = tf.io.parse_single_example(record, name_to_features)

        # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
        # So cast all int64 to int32.
        for name in list(example.keys()):
            t = example[name]
            if t.dtype == tf.int64:
                t = tf.cast(t, tf.int32)
            example[name] = t

        return example
        
    dataset = dataset.apply(
        tf.data.experimental.map_and_batch(
          lambda record: _decode_record(record, name_to_features),
          batch_size=BATCH_SIZE,
          drop_remainder=drop_remainder,
          num_parallel_calls=tf.data.experimental.AUTOTUNE))

    dataset.cache()

    if is_training:
        dataset = dataset.shuffle(seed=42,
                                  buffer_size=1000,
                                  reshuffle_each_iteration=True)

    return dataset


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

#    parser.add_argument('--model-type', type=str, default='bert')
#    parser.add_argument('--model-name', type=str, default='bert-base-cased')
    parser.add_argument('--train-data', 
                        type=str, 
                        default=os.environ['SM_CHANNEL_TRAIN'])
    parser.add_argument('--validation-data', 
                        type=str, 
                        default=os.environ['SM_CHANNEL_VALIDATION'])
    parser.add_argument('--model-dir', 
                        type=str, 
                        default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--hosts', 
                        type=list, 
                        default=json.loads(os.environ['SM_HOSTS']))
    parser.add_argument('--current-host', 
                        type=str, 
                        default=os.environ['SM_CURRENT_HOST'])    
    parser.add_argument('--num-gpus', 
                        type=int, 
                        default=os.environ['SM_NUM_GPUS'])

    args, _ = parser.parse_known_args()
#    model_type = args.model_type
#    model_name = args.model_name
    train_data = args.train_data
    validation_data = args.validation_data
    model_dir = args.model_dir
    hosts = args.hosts
    current_host = args.current_host
    num_gpus = args.num_gpus

    tokenizer = None
    config = None
    model = None

    # This is required when launching many instances at once...  the urllib request seems to get denied periodically
    successful_download = False
    retries = 0
    while (retries < 5 and not successful_download):
        try:
            tokenizer = DistilBertTokenizer.from_pretrained('distilbert-base-uncased')
            config = DistilBertConfig.from_pretrained('distilbert-base-uncased',
                                                      num_labels=len(CLASSES))
            model = TFDistilBertForSequenceClassification.from_pretrained('distilbert-base-uncased', 
                                                                          config=config)
            successful_download = True
            print('Sucessfully downloaded after {} retries.'.format(retries))
        except:
            retries = retries + 1
            random_sleep = random.randint(1, 30)
            print('Retry #{}.  Sleeping for {} seconds'.format(retries, random_sleep))
            time.sleep(random_sleep)

    if not tokenizer or not model or not config:
        print('Not properly initialized...')
    
    pipe_mode_str = os.environ.get('SM_INPUT_DATA_CONFIG', '')
    print('pipe_mode_str {}'.format(pipe_mode_str))
    pipe_mode = (pipe_mode_str.find('Pipe') >= 0)
    print('pipe_mode {}'.format(pipe_mode))

    train_data_filenames = glob('{}/*.tfrecord'.format(train_data))
    print('train_data_filenames {}'.format(train_data_filenames))
    train_dataset = file_based_input_dataset_builder(
        channel='train',
        input_filenames=train_data_filenames,
        pipe_mode=pipe_mode,
        is_training=True,
        drop_remainder=False).map(select_data_and_label_from_record)

    validation_data_filenames = glob('{}/*.tfrecord'.format(validation_data))
    print('validation_data_filenames {}'.format(validation_data_filenames))
    validation_dataset = file_based_input_dataset_builder(
        channel='validation',
        input_filenames=validation_data_filenames,
        pipe_mode=pipe_mode,
        is_training=False,
        drop_remainder=False).map(select_data_and_label_from_record)

    tf.config.optimizer.set_jit(USE_XLA)
    tf.config.optimizer.set_experimental_options({"auto_mixed_precision": USE_AMP})

    optimizer = tf.keras.optimizers.Adam(learning_rate=3e-5, epsilon=1e-08)
    if USE_AMP:
        # loss scaling is currently required when using mixed precision
        optimizer = tf.keras.mixed_precision.experimental.LossScaleOptimizer(optimizer, 'dynamic')

    loss = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)
    metric = tf.keras.metrics.SparseCategoricalAccuracy('accuracy')
    model.compile(optimizer=optimizer, loss=loss, metrics=[metric])
#    model.layers[0].trainable=False
    model.summary()

    log_dir = './tensorboard/classification/'
    tensorboard_callback = tf.keras.callbacks.TensorBoard(log_dir=log_dir)

    history = model.fit(train_dataset,
                        shuffle=True,
                        epochs=EPOCHS,
                        steps_per_epoch=STEPS_PER_EPOCH,
#                        validation_data=validation_dataset,
#                        validation_steps=VALIDATION_STEPS,
                        callbacks=[tensorboard_callback])

    print('Trained model {}'.format(model))

    # Save the Model
    model.save_pretrained(model_dir)

    loaded_model = TFDistilBertForSequenceClassification.from_pretrained(model_dir,
                                                                   id2label={
                                                                    0: 1,
                                                                    1: 2,
                                                                    2: 3,
                                                                    3: 4,
                                                                    4: 5
                                                                   },
                                                                   label2id={
                                                                    1: 0,
                                                                    2: 1,
                                                                    3: 2,
                                                                    4: 3,
                                                                    5: 4
                                                                   })

    tokenizer = DistilBertTokenizer.from_pretrained('distilbert-base-uncased')

    inference_pipeline = TextClassificationPipeline(model=loaded_model, 
                                                    tokenizer=tokenizer,
                                                    framework='tf',
                                                    device=-1) # -1 is CPU, >= 0 is GPU

    print("""I loved it!  I will recommend this to everyone.""", inference_pipeline("""I loved it!  I will recommend this to everyone."""))
    print("""Really bad.  I hope they don't make this anymore.""", inference_pipeline("""Really bad.  I hope they don't make this anymore."""))
    print("""It's OK.""", inference_pipeline("""It's OK."""))
