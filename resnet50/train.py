from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import functools
import numpy as np
import time
import commands
import subprocess
import threading

import cProfile
import pstats
import StringIO

import paddle
import paddle.fluid as fluid
import paddle.fluid.core as core
import paddle.fluid.profiler as profiler

import models
import models.resnet

# from continuous_evaluation import tracking_kpis


def parse_args():
    parser = argparse.ArgumentParser('Convolution model benchmark.')
    parser.add_argument(
        '--model',
        type=str,
        choices=['resnet_imagenet', 'resnet_cifar10'],
        default='resnet_imagenet',
        help='The model architecture.')
    parser.add_argument(
        '--batch_size', type=int, default=32, help='The minibatch size.')
    parser.add_argument(
        '--log_dir',
        '-f',
        type=str,
        default='./',
        help='The path of the log file')
    parser.add_argument(
        '--use_fake_data',
        action='store_true',
        help='use real data or fake data')
    parser.add_argument(
        '--skip_batch_num',
        type=int,
        default=5,
        help='The first num of minibatch num to skip, for better performance test'
    )
    parser.add_argument(
        '--iterations',
        type=int,
        default=80,
        help='The number of minibatches.')
    parser.add_argument(
        '--pass_num', type=int, default=100, help='The number of passes.')
    parser.add_argument(
        '--data_format',
        type=str,
        default='NCHW',
        choices=['NCHW', 'NHWC'],
        help='The data data_format, now only support NCHW.')
    parser.add_argument(
        '--device',
        type=str,
        default='GPU',
        choices=['CPU', 'GPU'],
        help='The device type.')
    parser.add_argument(
        "--gpu_id",
        type=int,
        default=3,
        help="The GPU Card Id. (default: %(default)d)")
    parser.add_argument(
        '--data_set',
        type=str,
        default='flowers',
        choices=['cifar10', 'flowers'],
        help='Optional dataset for benchmark.')

    args = parser.parse_args()
    return args


def print_arguments(args):
    print('-----------  Configuration Arguments -----------')
    for arg, value in sorted(vars(args).iteritems()):
        print('%s: %s' % (arg, value))
    print('------------------------------------------------')


def init_reader(args):
    train_reader = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.cifar.train10()
            if args.data_set == 'cifar10' else paddle.dataset.flowers.train(),
            buf_size=5120),
        batch_size=args.batch_size)

    test_reader = paddle.batch(
        paddle.dataset.cifar.test10()
        if args.data_set == 'cifar10' else paddle.dataset.flowers.test(),
        batch_size=args.batch_size)
    return train_reader, test_reader


def run_benchmark(model, args):
    if args.data_set == "cifar10":
        class_dim = 10
        if args.data_format == 'NCHW':
            dshape = [3, 32, 32]
        else:
            dshape = [32, 32, 3]
    else:
        class_dim = 102
        if args.data_format == 'NCHW':
            dshape = [3, 224, 224]
        else:
            dshape = [224, 224, 3]

    input = fluid.layers.data(name='data', shape=dshape, dtype='float32')
    label = fluid.layers.data(name='label', shape=[1], dtype='int64')
    predict = model(input, class_dim)
    cost = fluid.layers.cross_entropy(input=predict, label=label)
    avg_cost = fluid.layers.mean(x=cost)

    batch_size_tensor = fluid.layers.create_tensor(dtype='int64')
    batch_acc = fluid.layers.accuracy(
        input=predict, label=label, total=batch_size_tensor)

    inference_program = fluid.default_main_program().clone()
    with fluid.program_guard(inference_program):
        inference_program = fluid.io.get_inference_program(
            target_vars=[batch_acc, batch_size_tensor])

    optimizer = fluid.optimizer.Momentum(learning_rate=0.01, momentum=0.9)
    opts = optimizer.minimize(avg_cost)
    fluid.memory_optimize(fluid.default_main_program())

    train_reader, test_reader = init_reader()

    def test(exe):
        test_accuracy = fluid.average.WeightedAverage()
        for batch_id, data in enumerate(test_reader()):
            img_data = np.array(map(lambda x: x[0].reshape(dshape),
                                    data)).astype("float32")
            y_data = np.array(map(lambda x: x[1], data)).astype("int64")
            y_data = y_data.reshape([-1, 1])

            acc, weight = exe.run(inference_program,
                                  feed={"data": img_data,
                                        "label": y_data},
                                  fetch_list=[batch_acc, batch_size_tensor])
            test_accuracy.add(value=acc, weight=weight)

        return test_accuracy.eval()

    place = core.CPUPlace() if args.device == 'CPU' else core.CUDAPlace(0)
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())

    accuracy = fluid.average.WeightedAverage()

    if args.use_fake_data:
        data = train_reader().next()
        image = np.array(map(lambda x: x[0].reshape(dshape), data)).astype(
            'float32')
        label = np.array(map(lambda x: x[1], data)).astype('int64')
        label = label.reshape([-1, 1])

    im_num = 0
    total_train_time = 0.0
    total_iters = 0

    # train_acc_kpi = None
    # for kpi in tracking_kpis:
    #     if kpi.name == '%s_%s_train_acc' % (args.data_set, args.batch_size):
    #         train_acc_kpi = kpi
    # train_speed_kpi = None
    # for kpi in tracking_kpis:
    #     if kpi.name == '%s_%s_train_speed' % (args.data_set, args.batch_size):
    #         train_speed_kpi = kpi

    for pass_id in range(args.pass_num):
        every_pass_loss = []
        accuracy.reset()
        iter = 0
        pass_duration = 0.0
        for batch_id, data in enumerate(train_reader()):
            batch_start = time.time()
            if iter == args.iterations:
                break
            if not args.use_fake_data:
                image = np.array(map(lambda x: x[0].reshape(dshape),
                                     data)).astype('float32')
                label = np.array(map(lambda x: x[1], data)).astype('int64')
                label = label.reshape([-1, 1])
            loss, acc, weight = exe.run(
                fluid.default_main_program(),
                feed={'data': image,
                      'label': label},
                fetch_list=[avg_cost, batch_acc, batch_size_tensor])
            accuracy.add(value=acc, weight=weight)
            if iter >= args.skip_batch_num or pass_id != 0:
                batch_duration = time.time() - batch_start
                pass_duration += batch_duration
                im_num += label.shape[0]
            every_pass_loss.append(loss)
            # print("Pass: %d, Iter: %d, loss: %s, acc: %s" %
            #      (pass_id, iter, str(loss), str(acc)))
            iter += 1
            total_iters += 1

        total_train_time += pass_duration
        pass_train_acc = accuracy.eval()
        pass_test_acc = test(exe)
        print(
            "Pass:%d, Loss:%f, Train Accuray:%f, Test Accuray:%f, Handle Images Duration: %f\n"
            % (pass_id, np.mean(every_pass_loss), pass_train_acc,
               pass_test_acc, pass_duration))

    # if pass_id == args.pass_num - 1 and args.data_set == 'cifar10':
    #     train_acc_kpi.add_record(np.array(pass_train_acc, dtype='float32'))
    #     train_acc_kpi.persist()

    # if total_train_time > 0.0 and iter != args.skip_batch_num:
    #     examples_per_sec = im_num / total_train_time
    #     sec_per_batch = total_train_time / \
    #         (iter * args.pass_num - args.skip_batch_num)
    #     train_speed_kpi.add_record(np.array(examples_per_sec, dtype='float32'))

    # train_speed_kpi.persist()

    print('\nTotal examples: %d, total time: %.5f' %
          (im_num, total_train_time))
    print('%.5f examples/sec, %.5f sec/batch \n' %
          (examples_per_sec, sec_per_batch))

    # if args.use_cprof:
    #     pr.disable()
    #     s = StringIO.StringIO()
    #     sortby = 'cumulative'
    #     ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
    #     ps.print_stats()
    #     print(s.getvalue())


def collect_gpu_memory_data(alive):
    """
    collect the GPU memory data
    """
    global is_alive
    status, output = commands.getstatusoutput('rm -rf memory.txt')
    if status == 0:
        print('del memory.txt')
    command = "nvidia-smi --id=%s --query-compute-apps=used_memory --format=csv -lms 1 > memory.txt" % args.gpu_id
    p = subprocess.Popen(command, shell=True)
    if p.pid < 0:
        print('Get GPU memory data error')
    while (is_alive):
        time.sleep(1)
    p.kill()


# def save_gpu_data(mem_list):
#     gpu_memory_kpi = None
#     for kpi in tracking_kpis:
#         if kpi.name == '%s_%s_gpu_memory' % (args.data_set, args.batch_size):
#             gpu_memory_kpi = kpi
#     gpu_memory_kpi.add_record(max(mem_list))
#     gpu_memory_kpi.persist()

if __name__ == '__main__':
    model_map = {
        'resnet_imagenet': models.resnet.resnet_imagenet,
        'resnet_cifar10': models.resnet.resnet_cifar10
    }

    args = parse_args()
    print_arguments(args)

    global is_alive
    is_alive = True

    if args.data_format == 'NHWC':
        raise ValueError('Only support NCHW data_format now.')

    # if args.device == 'GPU':
    #     collect_memory_thread = threading.Thread(
    #         target=collect_gpu_memory_data, args=(is_alive, ))
    #     collect_memory_thread.setDaemon(True)
    #     collect_memory_thread.start()

    run_benchmark(model_map[args.model], args)

    is_alive = False
