import os, sys
import os.path as osp
import time
import random
import argparse
import numpy as np
from PIL import Image
import pprint

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from torchvision.utils import save_image,make_grid
from torch.utils.tensorboard import SummaryWriter
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.utils.data.distributed import DistributedSampler
import multiprocessing as mp

ROOT_PATH = osp.abspath(osp.join(osp.dirname(osp.abspath(__file__)),  ".."))
sys.path.insert(0, ROOT_PATH)
from lib.utils import mkdir_p,get_rank,merge_args_yaml,get_time_stamp,save_args
from lib.utils import load_models_opt,save_models_opt,save_models,load_npz,params_count
from lib.perpare import prepare_dataloaders,prepare_models
from lib.modules import sample_one_batch as sample, test as test, train as train
from lib.datasets import get_fix_data



def parse_args():
    # Training settings
    parser = argparse.ArgumentParser(description='Text2Img')
    parser.add_argument('--cfg', dest='cfg_file', type=str, 
                        default='',
                        help='optional config file')
    parser.add_argument('--num_workers', type=int, default=4,
                        help='number of workers(default: {0})'.format(mp.cpu_count() - 1))
    parser.add_argument('--stamp', type=str, default='normal',
                        help='the stamp of model')
    parser.add_argument('--pretrained_model_path', type=str, 
                        default='model',
                        help='the model for training')
    parser.add_argument('--log_dir', type=str, default='new',
                        help='file path to log directory')
    parser.add_argument('--model', type=str, default='RATLIP',
                        help='the model for training')
    parser.add_argument('--state_epoch', type=int, default=1,
                        help='state epoch')
    parser.add_argument('--batch_size', type=int, default=1,
                        help='batch size')
    parser.add_argument('--train', type=str, default='True',
                        help='if train model')
    parser.add_argument('--mixed_precision', type=str, default='False',
                        help='if use multi-gpu')
    parser.add_argument('--multi_gpus', type=str, default='False',
                        help='if use multi-gpu')
    parser.add_argument('--gpu_id', type=int, default=0,
                        help='gpu id')
    parser.add_argument('--local_rank', default=-1, type=int,
                        help='node rank for distributed training')
    parser.add_argument('--random_sample', action='store_true',default=True, 
                        help='whether to sample the dataset with random sampler')
    args = parser.parse_args()
    return args

# main函数
def main(args):
    time_stamp = get_time_stamp()
    stamp = '_'.join([str(args.model),'nf'+str(args.nf),
                      str(args.stamp),str(args.CONFIG_NAME),str(args.imsize),time_stamp])
    args.model_save_file = osp.join(ROOT_PATH, 'saved_models', str(args.CONFIG_NAME), stamp)
    log_dir = args.log_dir
    if log_dir == 'new':
        log_dir = osp.join(ROOT_PATH, 'logs/{0}'.\
                            format(osp.join(str(args.CONFIG_NAME), 'train', stamp)))
    args.img_save_dir = osp.join(ROOT_PATH, 'imgs/{0}'.\
                            format(osp.join(str(args.CONFIG_NAME), 'train', stamp)))
    if (args.multi_gpus==True) and (get_rank() != 0):
        None
    else:
        mkdir_p(osp.join(ROOT_PATH, 'logs'))
        mkdir_p(args.model_save_file)
        mkdir_p(args.img_save_dir)
    # prepare TensorBoard
    if (args.multi_gpus==True) and (get_rank() != 0):
        writer = None
    else:
        writer = SummaryWriter(log_dir)
    # prepare dataloader, models, data
    train_dl, valid_dl ,train_ds, valid_ds, sampler = prepare_dataloaders(args)
    CLIP4trn, CLIP4evl, image_encoder, text_encoder, netG, netD, netC = prepare_models(args)
    print('**************G_paras: ',params_count(netG))
    print('**************D_paras: ',params_count(netD)+params_count(netC))
    fixed_img, fixed_sent, fixed_words, fixed_z = \
    get_fix_data(train_dl, valid_dl, text_encoder, args)
    if (args.multi_gpus==True) and (get_rank() != 0):
        None
    else:
        fixed_grid = make_grid(fixed_img.cpu(), nrow=8, normalize=True)
        #writer.add_image('fixed images', fixed_grid, 0)
        img_name = 'gt.png'
        img_save_path = osp.join(args.img_save_dir, img_name)
        vutils.save_image(fixed_img.data, img_save_path, nrow=8, normalize=True)
    # prepare optimizer
    D_params = list(netD.parameters()) + list(netC.parameters())
    optimizerD = torch.optim.Adam(D_params, lr=args.lr_d, betas=(0.0, 0.9))
    optimizerG = torch.optim.Adam(netG.parameters(), lr=args.lr_g, betas=(0.0, 0.9))
    if args.mixed_precision==True:
        scaler_D = torch.cuda.amp.GradScaler(growth_interval=args.growth_interval)
        scaler_G = torch.cuda.amp.GradScaler(growth_interval=args.growth_interval)
    else:
        scaler_D = None
        scaler_G = None
    m1, s1 = load_npz(args.npz_path) #
    start_epoch = 1
    # load from checkpoint
    if args.state_epoch!=1:
        start_epoch = args.state_epoch + 1
        path = osp.join(args.pretrained_model_path, 
                        'state_epoch_%03d.pth'%(args.state_epoch))
        netG, netD, netC, optimizerG, optimizerD = \
            load_models_opt(netG, netD, netC, optimizerG, optimizerD, path, args.multi_gpus)
        #netG, netD, netC = load_model(netG, netD, netC, path, args.multi_gpus)
    # print args
    if (args.multi_gpus==True) and (get_rank() != 0):
        None
    else:
        pprint.pprint(args)
        arg_save_path = osp.join(log_dir, 'args.yaml')
        save_args(arg_save_path, args)
        print("Start Training")
    # Start training
    test_interval,gen_interval,save_interval = args.test_interval,\
        args.gen_interval,args.save_interval
    #torch.cuda.empty_cache()
    # start_epoch = 1
    for epoch in range(start_epoch, args.max_epoch, 1):
        if (args.multi_gpus==True):
            sampler.set_epoch(epoch)
        start_t = time.time()
        # training
        args.current_epoch = epoch
        torch.cuda.empty_cache()
        train(train_dl, netG, netD, netC, text_encoder, 
              image_encoder, optimizerG, optimizerD, scaler_G, scaler_D, args)
        torch.cuda.empty_cache()
        # save
        if epoch%save_interval==0:
            save_models_opt(netG, netD, netC, optimizerG, 
                            optimizerD, epoch, args.multi_gpus, args.model_save_file)
            torch.cuda.empty_cache()
        # sample
        if epoch%gen_interval==0:
            sample(fixed_z, fixed_sent, netG, args.multi_gpus, epoch, 
                   args.img_save_dir, writer)
            torch.cuda.empty_cache()
        # test
        if epoch%test_interval==0:
            #torch.cuda.empty_cache()
            FID, TI_score = test(valid_dl, text_encoder, netG, CLIP4evl, 
                                 args.device, m1, s1, epoch, args.max_epoch, 
                                 args.sample_times, args.z_dim, args.batch_size)
            torch.cuda.empty_cache()
        if (args.multi_gpus==True) and (get_rank() != 0):
            None
        else:
            if epoch%test_interval==0:
                writer.add_scalar('FID', FID, epoch)
                writer.add_scalar('CLIP_Score', TI_score, epoch)
                print('The %d epoch CLIP_Score: %.2f' % (epoch,TI_score*100))
            end_t = time.time()
            print('The epoch %d costs %.2fs'%(epoch, end_t-start_t))
            print('='*40)



if __name__ == "__main__":
    args = merge_args_yaml(parse_args())
    # device_count = torch.cuda.device_count()
    # set seed
    if args.manual_seed is None:
        args.manual_seed = 100
        #args.manualSeed = random.randint(1, 10000)
    random.seed(args.manual_seed)
    np.random.seed(args.manual_seed)
    torch.manual_seed(args.manual_seed)
    if args.cuda:
        if args.multi_gpus:
            torch.cuda.manual_seed_all(args.manual_seed)
            torch.distributed.init_process_group(backend="nccl")
            local_rank = torch.distributed.get_rank()
            torch.cuda.set_device(local_rank)
            args.device = torch.device("cuda", local_rank)
            args.local_rank = local_rank
        else:
            torch.cuda.manual_seed_all(args.manual_seed)
            torch.cuda.set_device(args.gpu_id)
            args.device = torch.device("cuda")
    else:
        args.device = torch.device('cpu')
    main(args)

