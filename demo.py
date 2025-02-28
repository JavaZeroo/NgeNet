import argparse
import copy
import numpy as np
import os
import yaml
import torch
from easydict import EasyDict as edict
import open3d as o3d
import sys
from alive_progress import alive_it
import shutil
from tqdm import tqdm

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ["CUDA_VISIBLE_DEVICES"] ="0" 

from data import collate_fn
from models import architectures, NgeNet, vote
from utils import decode_config, npy2pcd, pcd2npy, execute_global_registration, \
                  npy2feat, setup_seed, get_blue, get_green, voxel_ds, normal, \
                  read_cloud, vis_plys, get_red

CUR = os.path.dirname(os.path.abspath(__file__))


class NgeNet_pipeline():
    def __init__(self, ckpt_path, voxel_size, vote_flag, cuda=True):
        self.voxel_size_3dmatch = 0.025
        self.voxel_size = voxel_size
        self.scale = self.voxel_size / self.voxel_size_3dmatch
        self.cuda = cuda
        self.vote_flag = vote_flag
        config = self.prepare_config()
        self.neighborhood_limits = [38, 36, 35, 38]
        model = NgeNet(config)
        if self.cuda:
            model = model.cuda()
            model.load_state_dict(torch.load(ckpt_path))
        else:
            model.load_state_dict(
                torch.load(ckpt_path, map_location=torch.device('cpu')))
        self.model = model
        self.config = config
        self.model.eval()
    
    def prepare_config(self):
        config = decode_config(os.path.join(CUR, 'configs', 'threedmatch.yaml'))
        config = edict(config)
        # config.first_subsampling_dl = self.voxel_size
        config.architecture = architectures[config.dataset]
        return config

    def prepare_inputs(self, source, target):
        src_pcd_input = pcd2npy(voxel_ds(copy.deepcopy(source), self.voxel_size))
        tgt_pcd_input = pcd2npy(voxel_ds(copy.deepcopy(target), self.voxel_size))

        src_pcd_input /= self.scale
        tgt_pcd_input /= self.scale

        src_feats = np.ones_like(src_pcd_input[:, :1])
        tgt_feats = np.ones_like(tgt_pcd_input[:, :1])

        src_pcd = normal(npy2pcd(src_pcd_input), radius=4*self.voxel_size_3dmatch, max_nn=30, loc=(0, 0, 0))
        tgt_pcd = normal(npy2pcd(tgt_pcd_input), radius=4*self.voxel_size_3dmatch, max_nn=30, loc=(0, 0, 0))
        src_normals = np.array(src_pcd.normals).astype(np.float32) 
        tgt_normals = np.array(tgt_pcd.normals).astype(np.float32)

        T = np.eye(4)
        coors = np.array([[0, 0], [1, 1]])
        src_pcd = pcd2npy(source)
        tgt_pcd = pcd2npy(target)

        pair = dict(
            src_points=src_pcd_input,
            tgt_points=tgt_pcd_input,
            src_feats=src_feats,
            tgt_feats=tgt_feats,
            src_normals=src_normals,
            tgt_normals=tgt_normals,
            transf=T,
            coors=coors,
            src_points_raw=src_pcd,
            tgt_points_raw=tgt_pcd)
        
        dict_inputs = collate_fn([pair], self.config, self.neighborhood_limits)
        if self.cuda:
            for k, v in dict_inputs.items():
                    if isinstance(v, list):
                        for i in range(len(v)):
                            dict_inputs[k][i] = dict_inputs[k][i].cuda()
                    else:
                        dict_inputs[k] = dict_inputs[k].cuda()
        
        return dict_inputs

    def pipeline(self, source, target, npts=20000):
        inputs = self.prepare_inputs(source, target)
        batched_feats_h, batched_feats_m, batched_feats_l = self.model(inputs)
        stack_points = inputs['points']
        stack_lengths = inputs['stacked_lengths']
        coords_src = stack_points[0][:stack_lengths[0][0]]
        coords_tgt = stack_points[0][stack_lengths[0][0]:]
        feats_src_h = batched_feats_h[:stack_lengths[0][0]]
        feats_tgt_h = batched_feats_h[stack_lengths[0][0]:]
        feats_src_m = batched_feats_m[:stack_lengths[0][0]]
        feats_tgt_m = batched_feats_m[stack_lengths[0][0]:]
        feats_src_l = batched_feats_l[:stack_lengths[0][0]]
        feats_tgt_l = batched_feats_l[stack_lengths[0][0]:]

        source_npy = coords_src.detach().cpu().numpy() * self.scale
        target_npy = coords_tgt.detach().cpu().numpy() * self.scale

        source_feats_h = feats_src_h[:, :-2].detach().cpu().numpy()
        target_feats_h = feats_tgt_h[:, :-2].detach().cpu().numpy()
        source_feats_m = feats_src_m.detach().cpu().numpy()
        target_feats_m = feats_tgt_m.detach().cpu().numpy()
        source_feats_l = feats_src_l.detach().cpu().numpy()
        target_feats_l = feats_tgt_l.detach().cpu().numpy() 

        source_overlap_scores = feats_src_h[:, -2].detach().cpu().numpy()
        target_overlap_scores = feats_tgt_h[:, -2].detach().cpu().numpy()
        source_scores = source_overlap_scores
        target_scores = target_overlap_scores

        npoints = npts
        if npoints > 0:
            if source_npy.shape[0] > npoints:
                p = source_scores / np.sum(source_scores)
                idx = np.random.choice(len(source_npy), size=npoints, replace=False, p=p)
                source_npy = source_npy[idx]
                source_feats_h = source_feats_h[idx]
                source_feats_m = source_feats_m[idx]
                source_feats_l = source_feats_l[idx]
            
            if target_npy.shape[0] > npoints:
                p = target_scores / np.sum(target_scores)
                idx = np.random.choice(len(target_npy), size=npoints, replace=False, p=p)
                target_npy = target_npy[idx]
                target_feats_h = target_feats_h[idx]
                target_feats_m = target_feats_m[idx]
                target_feats_l = target_feats_l[idx]
        
        if self.vote_flag:
            after_vote = vote(source_npy=source_npy, 
                            target_npy=target_npy, 
                            source_feats=[source_feats_h, source_feats_m, source_feats_l], 
                            target_feats=[target_feats_h, target_feats_m, target_feats_l], 
                            voxel_size=self.voxel_size * 2,
                            use_cuda=self.cuda)
            source_npy, target_npy, source_feats_npy, target_feats_npy = after_vote
        else:
            source_feats_npy, target_feats_npy = source_feats_h, target_feats_h
        source, target = npy2pcd(source_npy), npy2pcd(target_npy)
        source_feats, target_feats = npy2feat(source_feats_npy), npy2feat(target_feats_npy)
        pred_T, estimate = execute_global_registration(source=source,
                                                       target=target,
                                                       source_feats=source_feats,
                                                       target_feats=target_feats,
                                                       voxel_size=self.voxel_size*2)
        
        torch.cuda.empty_cache()
        return pred_T


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Configuration Parameters')
    parser.add_argument('--src_path', required=True, help='source point cloud path')
    parser.add_argument('--tgt_path', required=False, help='target point cloud path')
    parser.add_argument('--checkpoint', default='your_path/3dmatch.pth', help='checkpoint path')
    parser.add_argument('--voxel_size', type=float, default=0.2, help='voxel size')
    parser.add_argument('--npts', type=int, default=80000,
                        help='the number of sampled points for registration')
    parser.add_argument('--no_vote', action='store_true',
                        help='whether to use multi-level consistent voting')
    parser.add_argument('--no_vis', action='store_true',
                        help='whether to visualize the point clouds')
    parser.add_argument('--no_cuda', action='store_true',
                        help='whether to use cuda')
    args = parser.parse_args()
    # input data 
    src_path=args.src_path
    print(src_path)

    files = os.listdir(src_path)
    # loading model
    cuda = not args.no_cuda
    vote_flag = not args.no_vote
    print(vote_flag)
    print('======================================')
    # shutil.rmtree('result')
    # os.mkdir('result')
    model = NgeNet_pipeline(
        ckpt_path=args.checkpoint, 
        voxel_size=args.voxel_size, 
        vote_flag=vote_flag,
        cuda=cuda)
    for index, file in enumerate(tqdm(files)):
        if index <=2306:
            continue
        file_dir = os.path.join(src_path, file)
        mid_dir = os.path.join(file_dir, 'center/1.pcd')
        up_dir = os.path.join(file_dir, 'up/1.pcd')
        down_dir = os.path.join(file_dir, 'down/1.pcd')
        target = read_cloud(mid_dir)
        pcds = [up_dir, down_dir]

        dic = {}

        output_dir = os.path.join('result', file)
        os.mkdir(output_dir)  
        for pcd in pcds:
            type = pcd.split('/')[-2]
            print(type)
            source = read_cloud(pcd)

            # registration
            T = model.pipeline(source, target, npts=args.npts)
            print(f"{type}_to_mid", T)
            # print(np.reshape(T, (1, -1))[0].tolist())
            dic[f"{type}_to_mid"] = np.reshape(T, (1, -1))[0].tolist()
            # vis
            estimate = copy.deepcopy(source).transform(T)
            # source.paint_uniform_color(get_red())
            # source.estimate_normals()
            # target.paint_uniform_color(get_green())
            # target.estimate_normals()



            # o3d.io.write_point_cloud("result/1.pcd",source, write_ascii=True)
            # o3d.io.write_point_cloud("result/2.pcd",target, write_ascii=True)
            estimate.paint_uniform_color(get_green())
            estimate.estimate_normals()
            o3d.io.write_point_cloud(os.path.join(output_dir, type + '.pcd'),estimate, write_ascii=True)
        os.system(f"cp {mid_dir} {os.path.join(output_dir, 'mid.pcd')}")
        with open(os.path.join(output_dir, 'calib.yaml'), 'w') as f:
            yaml.dump(dic, f)
        torch.cuda.empty_cache()