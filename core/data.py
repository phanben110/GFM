"""
Bridging Composite and Real: Towards End-to-end Deep Image Matting [IJCV-2021]
Dataset processing.

Copyright (c) 2021, Jizhizi Li (jili8515@uni.sydney.edu.au)
Licensed under the MIT License (see LICENSE for details)
Github repo: https://github.com/JizhiziLi/GFM
Paper repo (Arxiv): https://arxiv.org/abs/2010.16188

"""

from config import *
from util import *
import torch
import cv2
import os
import random
import numpy as np
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
import json
import logging
import pickle
from torchvision import transforms
from torch.autograd import Variable
from skimage.transform import resize


#########################
## pure functions
#########################
def trim_img(img):
	if img.ndim>2:
		img = img[:,:,0]
	return img

def resize_img(ori, img):
	img = resize(img, ori.shape)*255.0
	return img

def process_fgbg(ori, mask, is_fg, fgbg_path=None):
	if fgbg_path is not None:
		img = np.array(Image.open(fgbg_path))
	else:
		mask_3 = (mask/255.0)[:, :, np.newaxis].astype(np.float32)
		img = ori*mask_3 if is_fg else ori*(1-mask_3)
	return img

def add_guassian_noise(img, fg, bg):
	row,col,ch= img.shape
	mean = 0
	sigma = 10
	gauss = np.random.normal(mean,sigma,(row,col,ch))
	gauss = gauss.reshape(row,col,ch)
	noisy_img = np.uint8(img + gauss)
	noisy_fg = np.uint8(fg + gauss)
	noisy_bg = np.uint8(bg + gauss)
	return noisy_img, noisy_fg, noisy_bg

def generate_composite_rssn(fg, bg, mask, fg_denoise=None, bg_denoise=None):
	## resize bg accordingly
	h, w, c = fg.shape
	alpha = np.zeros((h, w, 1), np.float32)
	alpha[:, :, 0] = mask / 255.
	bg = resize_img(fg, bg)
	## use denoise fg/bg randomly
	if fg_denoise is not None and random.random()<0.5:
		fg = fg_denoise
		bg = resize_img(fg, bg_denoise)
	## reduce sharpness discrepancy
	if random.random()<0.5:
		rand_kernel = random.choice([20,30,40,50,60])
		bg = cv2.blur(bg, (rand_kernel,rand_kernel))
	composite = alpha * fg + (1 - alpha) * bg
	composite = composite.astype(np.uint8)
	## reduce noise discrepancy
	if random.random()<0.5:
		composite, fg, bg = add_guassian_noise(composite, fg, bg)
	return composite, fg, bg

def generate_composite_coco(fg, bg, mask):
	h, w, c = fg.shape
	alpha = np.zeros((h, w, 1), np.float32)
	alpha[:, :, 0] = mask / 255.
	bg = resize_img(fg, bg)
	composite = alpha * fg + (1 - alpha) * bg
	composite = composite.astype(np.uint8)
	return composite, fg, bg


def gen_trimap_with_dilate(alpha, kernel_size):	
	kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size,kernel_size))
	fg_and_unknown = np.array(np.not_equal(alpha, 0).astype(np.float32))
	fg = np.array(np.equal(alpha, 255).astype(np.float32))
	dilate =  cv2.dilate(fg_and_unknown, kernel, iterations=1)
	erode = cv2.erode(fg, kernel, iterations=1)
	trimap = erode *255 + (dilate-erode)*128
	return trimap.astype(np.uint8)

def gen_dilate(alpha, kernel_size): 
	kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size,kernel_size))
	fg_and_unknown = np.array(np.not_equal(alpha, 0).astype(np.float32))
	dilate =  cv2.dilate(fg_and_unknown, kernel, iterations=1)*255
	return dilate.astype(np.uint8)

def gen_erosion(alpha, kernel_size): 
	kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size,kernel_size))
	fg = np.array(np.equal(alpha, 255).astype(np.float32))
	erode = cv2.erode(fg, kernel, iterations=1)*255
	return erode.astype(np.uint8)


#########################
## Data transformer
#########################
class MattingTransform(object):
	def __init__(self):
		super(MattingTransform, self).__init__()

	def __call__(self, *argv):
		ori = argv[0]
		h, w, c = ori.shape
		rand_ind = random.randint(0, len(CROP_SIZE) - 1)
		crop_size = CROP_SIZE[rand_ind] if CROP_SIZE[rand_ind]<min(h, w) else 320
		resize_size = RESIZE_SIZE
		### generate crop centered in transition area randomly
		trimap = argv[1]
		trimap_crop = trimap[:h-crop_size, :w-crop_size]
		target = np.where(trimap_crop == 128) if random.random() < 0.5 else np.where(trimap_crop > -100)
		if len(target[0])==0:
			target = np.where(trimap_crop > -100)

		rand_ind = np.random.randint(len(target[0]), size = 1)[0]
		cropx, cropy = target[1][rand_ind], target[0][rand_ind]
		# # flip the samples randomly
		flip_flag=True if random.random()<0.5 else False
		# generate samples (crop, flip, resize)
		argv_transform = []
		for item in argv:
			item = item[cropy:cropy+crop_size, cropx:cropx+crop_size]
			if flip_flag:
				item = cv2.flip(item, 1)
			item = cv2.resize(item, (resize_size, resize_size), interpolation=cv2.INTER_LINEAR)
			argv_transform.append(item)

		return argv_transform


#########################
## Data Loader
#########################
class MattingDataset(torch.utils.data.Dataset):
	def __init__(self, args, transform):
		
		self.samples=[]
		self.transform = transform
		self.logging = args.logging
		self.BG_CHOICE = args.bg_choice
		self.backbone = args.backbone
		self.FG_CF = True if args.fg_generate=='closed_form' else False
		self.RSSN_DENOISE = args.rssn_denoise
		
		self.logging.info('===> Loading training set')
		self.samples += generate_paths_for_dataset(args)
		self.logging.info(f"\t--crop_size: {CROP_SIZE} | resize: {RESIZE_SIZE}")
		self.logging.info("\t--Valid Samples: {}".format(len(self.samples)))

	def __getitem__(self,index):
		# Prepare training sample paths
		ori_path = self.samples[index][0]
		mask_path = self.samples[index][1]
		fg_path = self.samples[index][2] if self.FG_CF else None
		bg_path = self.samples[index][3] if (self.FG_CF or self.BG_CHOICE!='original') else None
		fg_path_denoise = self.samples[index][4] if (self.BG_CHOICE=='hd' and self.RSSN_DENOISE) else None
		bg_path_denoise = self.samples[index][5] if (self.BG_CHOICE=='hd' and self.RSSN_DENOISE) else None
		# Prepare ori/mask/fg/bg (mandatary)
		ori = np.array(Image.open(ori_path))
		mask = trim_img(np.array(Image.open(mask_path)))
		fg = process_fgbg(ori, mask, True, fg_path)
		bg = process_fgbg(ori, mask, False, bg_path)
		# Prepare composite for hd/coco
		if self.BG_CHOICE == 'hd':
			fg_denoise = process_fgbg(ori, mask, True, fg_path_denoise) if self.RSSN_DENOISE else None
			bg_denoise = process_fgbg(ori, mask, True, bg_path_denoise) if self.RSSN_DENOISE else None
			ori, fg, bg = generate_composite_rssn(fg, bg, mask, fg_denoise, bg_denoise)
		elif self.BG_CHOICE == 'coco':
			ori, fg, bg = generate_composite_coco(fg, bg, mask)
		# Generate trimap/dilation/erosion online
		kernel_size = random.randint(25,35)
		trimap = gen_trimap_with_dilate(mask, kernel_size)
		dilation = gen_dilate(mask, kernel_size)
		erosion = gen_erosion(mask, kernel_size)
		# Data transformation to generate samples
		# crop/flip/resize
		argv = self.transform(ori, mask, fg, bg, trimap, dilation, erosion)

		argv_transform = []
		for item in argv:
			if item.ndim<3:
				item = torch.from_numpy(item.astype(np.float32)[np.newaxis, :, :])
			else:
				item = torch.from_numpy(item.astype(np.float32)).permute(2, 0, 1)
			argv_transform.append(item)

		[ori, mask, fg, bg, trimap, dilation, erosion] = argv_transform
		return ori, mask, fg, bg, trimap, dilation, erosion

	def __len__(self):
		return len(self.samples)
