import os
from pickle import TRUE
import shutil
from tkinter import NORMAL, SINGLE
os.environ['KMP_DUPLICATE_LIB_OK']='True'
import torch
import fire
import gradio as gr
from PIL import Image
from functools import partial

import cv2
import time
import numpy as np
from rembg import remove
from segment_anything import sam_model_registry, SamPredictor

import os
import sys
import numpy
import torch
import rembg
import threading
import urllib.request
from PIL import Image
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
import streamlit as st
import huggingface_hub
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection
from mvdiffusion.models.unet_mv2d_condition import UNetMV2DConditionModel
from mvdiffusion.data.single_image_dataset import SingleImageDataset as MVDiffusionDataset
from mvdiffusion.pipelines.pipeline_mvdiffusion_image import MVDiffusionImagePipeline
from diffusers import AutoencoderKL, DDPMScheduler, DDIMScheduler
from einops import rearrange
import numpy as np
import subprocess
from datetime import datetime

def save_image(tensor):
    ndarr = tensor.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).to("cpu", torch.uint8).numpy()
    # pdb.set_trace()
    im = Image.fromarray(ndarr)
    return ndarr


def save_image_to_disk(tensor, fp):
    ndarr = tensor.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).to("cpu", torch.uint8).numpy()
    # pdb.set_trace()
    im = Image.fromarray(ndarr)
    im.save(fp)
    return ndarr


def save_image_numpy(ndarr, fp):
    im = Image.fromarray(ndarr)
    im.save(fp)


def save_image_numpy_RGB(ndarr, fp):
    im = Image.fromarray(ndarr)
    img_w, img_h = im.size
    new_canvas = Image.new(mode="RGB", size = (img_w,img_h), color = (255, 255, 255))
    new_canvas.paste(im, mask=im.split()[3]) #paste with alpha mask
    new_canvas.save(fp)
    

weight_dtype = torch.float16

_TITLE = '''Wonder3D: Single Image to 3D using Cross-Domain Diffusion'''
_DESCRIPTION = '''
<div>
Generate consistent multi-view normals maps and color images.
<a style="display:inline-block; margin-left: .5em" href='https://github.com/xxlong0/Wonder3D/'><img src='https://img.shields.io/github/stars/xxlong0/Wonder3D?style=social' /></a>
</div>
<div>
The demo does not include the mesh reconstruction part, please visit <a href="https://github.com/xxlong0/Wonder3D/">our github repo</a> to get a textured mesh.
</div>
'''
_GPU_ID = 0


if not hasattr(Image, 'Resampling'):
    Image.Resampling = Image


def sam_init():
    sam_checkpoint = os.path.join(os.path.dirname(__file__), "sam_pt", "sam_vit_h_4b8939.pth")
    model_type = "vit_h"

    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint).to(device=f"cuda:{_GPU_ID}")
    predictor = SamPredictor(sam)
    return predictor


def sam_segment(predictor, input_image, *bbox_coords):
    bbox = np.array(bbox_coords)
    image = np.asarray(input_image)

    start_time = time.time()
    predictor.set_image(image)

    masks_bbox, scores_bbox, logits_bbox = predictor.predict(box=bbox, multimask_output=True)

    print(f"SAM Time: {time.time() - start_time:.3f}s")
    out_image = np.zeros((image.shape[0], image.shape[1], 4), dtype=np.uint8)
    out_image[:, :, :3] = image
    out_image_bbox = out_image.copy()
    out_image_bbox[:, :, 3] = masks_bbox[-1].astype(np.uint8) * 255
    torch.cuda.empty_cache()
    return Image.fromarray(out_image_bbox, mode='RGBA')


def expand2square(pil_img, background_color):
    width, height = pil_img.size
    if width == height:
        return pil_img
    elif width > height:
        result = Image.new(pil_img.mode, (width, width), background_color)
        result.paste(pil_img, (0, (width - height) // 2))
        return result
    else:
        result = Image.new(pil_img.mode, (height, height), background_color)
        result.paste(pil_img, ((height - width) // 2, 0))
        return result


def preprocess(predictor, input_image, chk_group=None, segment=True, rescale=False):
    RES = 1024
    input_image.thumbnail([RES, RES], Image.Resampling.LANCZOS)
    if chk_group is not None:
        segment = "Background Removal" in chk_group
        rescale = "Rescale" in chk_group
    if segment:
        image_rem = input_image.convert('RGBA')
        image_nobg = remove(image_rem, alpha_matting=True)
        arr = np.asarray(image_nobg)[:, :, -1]
        x_nonzero = np.nonzero(arr.sum(axis=0))
        y_nonzero = np.nonzero(arr.sum(axis=1))
        x_min = int(x_nonzero[0].min())
        y_min = int(y_nonzero[0].min())
        x_max = int(x_nonzero[0].max())
        y_max = int(y_nonzero[0].max())
        input_image = sam_segment(predictor, input_image.convert('RGB'), x_min, y_min, x_max, y_max)
    # Rescale and recenter
    if rescale:
        image_arr = np.array(input_image)
        in_w, in_h = image_arr.shape[:2]
        out_res = min(RES, max(in_w, in_h))
        ret, mask = cv2.threshold(np.array(input_image.split()[-1]), 0, 255, cv2.THRESH_BINARY)
        x, y, w, h = cv2.boundingRect(mask)
        max_size = max(w, h)
        ratio = 0.75
        side_len = int(max_size / ratio)
        padded_image = np.zeros((side_len, side_len, 4), dtype=np.uint8)
        center = side_len // 2
        padded_image[center - h // 2 : center - h // 2 + h, center - w // 2 : center - w // 2 + w] = image_arr[y : y + h, x : x + w]
        rgba = Image.fromarray(padded_image).resize((out_res, out_res), Image.LANCZOS)

        rgba_arr = np.array(rgba) / 255.0
        rgb = rgba_arr[..., :3] * rgba_arr[..., -1:] + (1 - rgba_arr[..., -1:])
        input_image = Image.fromarray((rgb * 255).astype(np.uint8))
    else:
        input_image = expand2square(input_image, (127, 127, 127, 0))
    return input_image, input_image.resize((320, 320), Image.Resampling.LANCZOS)


def load_wonder3d_pipeline(cfg):

    pipeline = MVDiffusionImagePipeline.from_pretrained(
    cfg.pretrained_model_name_or_path,
    torch_dtype=weight_dtype
    )

    # pipeline.to('cuda:0')
    pipeline.unet.enable_xformers_memory_efficient_attention()


    if torch.cuda.is_available():
        pipeline.to('cuda:0')
    # sys.main_lock = threading.Lock()
    return pipeline


from mvdiffusion.data.single_image_dataset import SingleImageDataset


def prepare_data(single_image, crop_size):
    dataset = SingleImageDataset(root_dir='', num_views=6, img_wh=[256, 256], bg_color='white', crop_size=crop_size, single_image=single_image)
    return dataset[0]

scene = 'scene'
realESRGANpath = 'realesrgan-ncnn-vulkan'

def run_pipeline(pipeline, cfg, single_image, guidance_scale, steps, seed, crop_size, writeoutput_chk_group=None, scale_chk_group=None):
    import pdb
    global scene
    # pdb.set_trace()

    if writeoutput_chk_group is not None:
        write_image = "Write Results" in writeoutput_chk_group    
        #replace_front = "Replace Frontview" in writeoutput_chk_group    
    if scale_chk_group is not None:
        x4Rescale = "Rescale by x4" in scale_chk_group
        x2Rescale = "Rescale by x2" in scale_chk_group
        
    batch = prepare_data(single_image, crop_size)

    #write_image = True

    pipeline.set_progress_bar_config(disable=True)
    seed = int(seed)
    generator = torch.Generator(device=pipeline.unet.device).manual_seed(seed)

    # repeat  (2B, Nv, 3, H, W)
    imgs_in = torch.cat([batch['imgs_in']] * 2, dim=0).to(weight_dtype)

    # (2B, Nv, Nce)
    camera_embeddings = torch.cat([batch['camera_embeddings']] * 2, dim=0).to(weight_dtype)

    task_embeddings = torch.cat([batch['normal_task_embeddings'], batch['color_task_embeddings']], dim=0).to(weight_dtype)

    camera_embeddings = torch.cat([camera_embeddings, task_embeddings], dim=-1).to(weight_dtype)

    # (B*Nv, 3, H, W)
    imgs_in = rearrange(imgs_in, "Nv C H W -> (Nv) C H W")
    # (B*Nv, Nce)
    # camera_embeddings = rearrange(camera_embeddings, "B Nv Nce -> (B Nv) Nce")

    out = pipeline(
        imgs_in,
        camera_embeddings,
        generator=generator,
        guidance_scale=guidance_scale,
        num_inference_steps=steps,
        output_type='pt',
        num_images_per_prompt=1,
        **cfg.pipe_validation_kwargs,
    ).images

    bsz = out.shape[0] // 2
    normals_pred = out[:bsz]
    images_pred = out[bsz:]
    
    #for rescaled color/normals
    images_rescaled = []
    normals_rescaled = []

    num_views = 6
    if write_image:
        VIEWS = ['front', 'front_right', 'right', 'back', 'left', 'front_left']
        cur_dir = os.path.join("./outputs", f"cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}")

        #scene = 'scene'+datetime.now().strftime('@%Y%m%d-%H%M%S')
        scene = 'scene'+datetime.now().strftime('--%Y%m%d-%H%M%S')
        scene_dir = os.path.join(cur_dir, scene)
        normal_dir = os.path.join(scene_dir, "normals")
        masked_colors_dir = os.path.join(scene_dir, "masked_colors")
        os.makedirs(normal_dir, exist_ok=True)
        os.makedirs(masked_colors_dir, exist_ok=True)
        for j in range(num_views):
            view = VIEWS[j]
            normal = normals_pred[j]
            color = images_pred[j]

            normal_filename = f"normals_000_{view}.png"
            rgb_filename = f"rgb_000_{view}.png"
            normal = save_image_to_disk(normal, os.path.join(normal_dir, normal_filename))
            color = save_image_to_disk(color, os.path.join(scene_dir, rgb_filename))
            '''
            if j==0 and replace_front: #swap front view color image with recentered highres image
                new_canvas = Image.new(mode="RGB", size = (256,256), color = (255, 255, 255))
                single_image_resized = single_image.resize( (int(crop_size) , int(crop_size) ), Image.Resampling.LANCZOS  )
                bg_w, bg_h = new_canvas.size
                img_w, img_h = single_image_resized.size
                offset = ((bg_w - img_w) // 2, (bg_h - img_h) // 2)
                new_canvas.paste(single_image_resized , offset,  mask=single_image_resized.split()[3]) #rescale to crop size x crop size, centered, with alpha mask
                new_canvas.save(os.path.join(scene_dir, rgb_filename))                
            else: 
                color = save_image_to_disk(color, os.path.join(scene_dir, rgb_filename))
            '''
            print(f'x4: {x4Rescale} /x2: {x2Rescale}' )
            #x4
            if x4Rescale:
                subprocess.run(
                    f'cd {realESRGANpath} && realesrgan-ncnn-vulkan -i .{os.path.join(normal_dir, normal_filename)} -o .{os.path.join(normal_dir, normal_filename)} -n 4x-ultrasharp && cd..',
                    shell=True,
                )
                subprocess.run(
                    f'cd {realESRGANpath} && realesrgan-ncnn-vulkan -i .{os.path.join(scene_dir, rgb_filename)} -o .{os.path.join(scene_dir, rgb_filename)} -n 4x-ultrasharp && cd..',
                    shell=True,
                )
                normalBGR =  cv2.imread(os.path.join(normal_dir, normal_filename))
                normal = cv2.cvtColor(normalBGR, cv2.COLOR_BGR2RGB)
                colorBGR = cv2.imread(os.path.join(scene_dir, rgb_filename))
                color = cv2.cvtColor(colorBGR, cv2.COLOR_BGR2RGB)
            #x2
            elif x2Rescale:
                subprocess.run(
                    f'cd {realESRGANpath} && realesrgan-ncnn-vulkan -i .{os.path.join(normal_dir, normal_filename)} -o .{os.path.join(normal_dir, normal_filename)} -s 2 -n BSRGANx2 && cd..',
                    shell=True,
                )
                subprocess.run(
                    f'cd {realESRGANpath} && realesrgan-ncnn-vulkan -i .{os.path.join(scene_dir, rgb_filename)} -o .{os.path.join(scene_dir, rgb_filename)} -s 2 -n BSRGANx2 && cd..',
                    shell=True,
                )
                normalBGR =  cv2.imread(os.path.join(normal_dir, normal_filename))
                normal = cv2.cvtColor(normalBGR, cv2.COLOR_BGR2RGB)
                colorBGR = cv2.imread(os.path.join(scene_dir, rgb_filename))
                color = cv2.cvtColor(colorBGR, cv2.COLOR_BGR2RGB)
            '''
            #if frontview is removed and no rescaling is done, numpy image is reloaded into color variable
            elif j==0 and replace_front: 
                colorBGR = cv2.imread(os.path.join(scene_dir, rgb_filename))
                color = cv2.cvtColor(colorBGR, cv2.COLOR_BGR2RGB)            
            '''
            rm_normal = remove(normal)
            rm_color = remove(color)

            save_image_numpy(rm_normal, os.path.join(scene_dir, normal_filename))
            save_image_numpy(rm_color, os.path.join(masked_colors_dir, rgb_filename))
            
            images_rescaled.append(rm_color)
            normals_rescaled.append(rm_normal)

    #normals_pred = [save_image(normals_pred[i]) for i in range(bsz)]
    #images_pred = [save_image(images_pred[i]) for i in range(bsz)]

    #images_rescaled and normals_rescaled are already numpy lists. no need for conversion
    #normals_rescaled = [save_image(normals_rescaled[i]) for i in range(bsz)]
    #images_rescaled = [save_image(images_rescaled[i]) for i in range(bsz)]
        
    #out = images_pred + normals_pred
    out = images_rescaled + normals_rescaled
    return out

def process_from_2dview(mode, data_dir, guidance_scale, crop_size, scale_chk_group, recon_chk_group, view_1, view_2, view_3, view_4, view_5, view_6, normal_1, normal_2, normal_3, normal_4, normal_5, normal_6):
    
    global scene
    #new scene dir name by the datetime
    scene = 'scene'+datetime.now().strftime('--%Y%m%d-%H%M%S')
    VIEWS = ['front', 'front_right', 'right', 'back', 'left', 'front_left']
    view_list=[view_1, view_2, view_3, view_4, view_5, view_6]
    normal_list=[normal_1, normal_2, normal_3, normal_4, normal_5, normal_6]

    
    #copy images to a new scene dir
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    root_path=os.path.join(cur_dir, f'{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/')
    scene_path = os.path.join(root_path,scene)
    maskedcolor_path = os.path.join(scene_path,'masked_colors')
    normal_dir = os.path.join(scene_path, "normals")
    
    #prepare dir tree
    os.makedirs( scene_path, exist_ok=True )
    os.makedirs( maskedcolor_path, exist_ok=True )
    os.makedirs( normal_dir, exist_ok=True )
    
    print(f'Created dir:')
    print(f'{scene_path}')
    print(f'{maskedcolor_path}')
    print(f'{normal_dir}')
    
    num_views = 6
    
    for j in range(num_views):
        view = VIEWS[j]
        normal_filename = f"normals_000_{view}.png"
        rgb_filename = f"rgb_000_{view}.png"
        #remove BG
        view_list[j]=remove(view_list[j])
        normal_list[j]=remove(normal_list[j])

        #copy color/normal images
        save_image_numpy_RGB(view_list[j], os.path.join(scene_path,rgb_filename) ) #RGB
        save_image_numpy(view_list[j], os.path.join(maskedcolor_path,rgb_filename) ) #RGBA
        save_image_numpy_RGB(normal_list[j], os.path.join(normal_dir,normal_filename) ) #RGB
        save_image_numpy(normal_list[j], os.path.join(scene_path,normal_filename) ) #RGBA
        
    #run 3d recon on root path/scene + 2d
    return process_3d(mode, data_dir, guidance_scale, crop_size, scale_chk_group, recon_chk_group)
    

def process_3d(mode, data_dir, guidance_scale, crop_size, scale_chk_group=None, recon_chk_group=None):
    
    dir = None
    global scene
    
    glb_files = None
    obj_files = None
    
    if scale_chk_group is not None:        
        x4Rescale = "Rescale by x4" in scale_chk_group
        x2Rescale = "Rescale by x2" in scale_chk_group
        
    if recon_chk_group is not None:        
        instantNSR = "Instant-NSR-PL" in recon_chk_group
        NeuS= "NeuS" in recon_chk_group
        no_recon = "No Reconstruction" in recon_chk_group
        
    if no_recon:
        return

    cur_dir = os.path.dirname(os.path.abspath(__file__))
        
    if instantNSR:
        if x4Rescale:
            subprocess.run(
               f'cd instant-nsr-pl && python launch.py --config configs/neuralangelo-ortho-wmask_1024.yaml --gpu 0 --train dataset.root_dir=../{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/ dataset.scene={scene} && cd ..',
                shell=True,
            )
        elif x2Rescale:
            subprocess.run(
               f'cd instant-nsr-pl && python launch.py --config configs/neuralangelo-ortho-wmask_512.yaml --gpu 0 --train dataset.root_dir=../{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/ dataset.scene={scene} && cd ..',
                shell=True,
            )
        else:
            subprocess.run(
               f'cd instant-nsr-pl && python launch.py --config configs/neuralangelo-ortho-wmask.yaml --gpu 0 --train dataset.root_dir=../{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/ dataset.scene={scene} && cd ..',
                shell=True,
            )
        import glob                
        obj_files = sorted( glob.glob(f'{cur_dir}/instant-nsr-pl/exp/mesh-ortho-{scene}/*/save/*.obj', recursive=True) , key=os.path.getctime)
        #print(f'{cur_dir}/instant-nsr-pl/exp/{scene}/')
        #print( glob.glob(f'{cur_dir}/instant-nsr-pl/exp/mesh-ortho-{scene}/*/save', recursive=True) )
        print(obj_files)
        
    elif NeuS:
        if x4Rescale:
            subprocess.run(
                f'cd NeuS && python exp_runner.py --mode train --conf ./confs/wmask_1024.conf --case {scene} --data_dir ../{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/ && cd ..',
                shell=True,
            )
        elif x2Rescale:
            subprocess.run(
                f'cd NeuS && python exp_runner.py --mode train --conf ./confs/wmask_512.conf --case {scene} --data_dir ../{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/ && cd ..',
                shell=True,
            )
        else:
            subprocess.run(
                f'cd NeuS && python exp_runner.py --mode train --conf ./confs/wmask.conf --case {scene} --data_dir ../{data_dir}/cropsize-{int(crop_size)}-cfg{guidance_scale:.1f}/ && cd ..',
                shell=True,
            )
        import glob
        # import pdb

        # pdb.set_trace()

        #obj_files = glob.glob(f'{cur_dir}/instant-nsr-pl/exp/{scene}/*/save/*.obj', recursive=True)
        glb_files = sorted( glob.glob(f'{cur_dir}/NeuS/exp/neus/{scene}/meshes/*.glb', recursive=True) , key=os.path.getctime)
        print(glb_files)
   
    
    if glb_files:
        dir = glb_files[-1]
    elif obj_files:
        dir = obj_files[-1]
    return dir


@dataclass
class TestConfig:
    pretrained_model_name_or_path: str
    pretrained_unet_path: str
    revision: Optional[str]
    validation_dataset: Dict
    save_dir: str
    seed: Optional[int]
    validation_batch_size: int
    dataloader_num_workers: int

    local_rank: int

    pipe_kwargs: Dict
    pipe_validation_kwargs: Dict
    unet_from_pretrained_kwargs: Dict
    validation_guidance_scales: List[float]
    validation_grid_nrow: int
    camera_embedding_lr_mult: float

    num_views: int
    camera_embedding_type: str

    pred_type: str  # joint, or ablation

    enable_xformers_memory_efficient_attention: bool

    cond_on_normals: bool
    cond_on_colors: bool


def run_demo():
    #from utils.misc import load_config
    from omegaconf import OmegaConf

    # parse YAML config to OmegaConf
    #cfg = load_config("./configs/mvdiffusion-joint-ortho-6views.yaml")
    cfg = OmegaConf.load("./configs/mvdiffusion-joint-ortho-6views.yaml")
    # print(cfg)
    schema = OmegaConf.structured(TestConfig)
    cfg = OmegaConf.merge(schema, cfg)

    pipeline = load_wonder3d_pipeline(cfg)
    torch.set_grad_enabled(False)
    pipeline.to(f'cuda:{_GPU_ID}')

    predictor = sam_init()

    custom_theme = gr.themes.Soft(primary_hue="blue").set(
        button_secondary_background_fill="*neutral_100", button_secondary_background_fill_hover="*neutral_200"
    )
    custom_css = '''#disp_image {
        text-align: center; /* Horizontally center the content */
    }'''

    with gr.Blocks(title=_TITLE, theme=custom_theme, css=custom_css) as demo:
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown('# ' + _TITLE)
        gr.Markdown(_DESCRIPTION)
        with gr.Row(variant='panel'):
            with gr.Column(scale=1):
                input_image = gr.Image(type='pil', image_mode='RGBA', height=320, label='Input image')#, tool=None)

            with gr.Column(scale=1):
                processed_image = gr.Image(
                    type='pil',
                    label="Processed Image",
                    interactive=False,
                    height=320,
                    #tool=None,
                    image_mode='RGBA',
                    elem_id="disp_image",
                    visible=True,
                )
            with gr.Column(scale=1):
                ## add 3D Model
                obj_3d = gr.Model3D(
                                    # clear_color=[0.0, 0.0, 0.0, 0.0], 
                                    label="3D Model", height=320, 
                                    # camera_position=[0,0,2.0]
                                    )
                processed_image_highres = gr.Image(type='pil', image_mode='RGBA', visible=False)#, tool=None)
        with gr.Row(variant='panel'):
            with gr.Column(scale=1):
                example_folder = os.path.join(os.path.dirname(__file__), "./example_images")
                example_fns = [os.path.join(example_folder, example) for example in os.listdir(example_folder)]
                gr.Examples(
                    examples=example_fns,
                    inputs=[input_image],
                    outputs=[input_image],
                    cache_examples=False,
                    label='Examples (click one of the images below to start)',
                    examples_per_page=30,
                )
            with gr.Column(scale=1):
                with gr.Accordion('Advanced options', open=True):
                    with gr.Row():
                        with gr.Column():
                            input_processing = gr.CheckboxGroup(
                                ['Background Removal'],
                                label='Input Image Preprocessing',
                                value=['Background Removal'],
                                info='untick this, if masked image with alpha channel',
                            )
                        with gr.Column():
                            output_processing = gr.CheckboxGroup(
                                ['Write Results'], label='write the results in ./outputs folder', value=['Write Results']
                            )
                        with gr.Column():
                            scale_processing = gr.Radio(
                                ['No Rescaling', 'Rescale by x2',  'Rescale by x4'], label='Rescale by x1/x2/x4', value='No Rescaling'
                            )  
                        with gr.Column():
                            recon_method = gr.Radio(
                                ['Instant-NSR-PL', 'NeuS', 'No Reconstruction'], label='Reconstruction methods', value='No Reconstruction'
                            )    
                    with gr.Row():
                        with gr.Column():
                            scale_slider = gr.Slider(1, 5, value=1, step=1, label='Classifier Free Guidance Scale')
                        with gr.Column():
                            steps_slider = gr.Slider(15, 100, value=50, step=1, label='Number of Diffusion Inference Steps')
                    with gr.Row():
                        with gr.Column():
                            seed = gr.Number(42, label='Seed')
                        with gr.Column():
                            crop_size = gr.Number(192, label='Crop size')

                        mode = gr.Textbox('train', visible=False)
                        data_dir = gr.Textbox('outputs', visible=False)
                    # crop_size = 192
                    # with gr.Row():
                    #     method = gr.Radio(choices=['instant-nsr-pl', 'NeuS'], label='Method (Default: instant-nsr-pl)', value='instant-nsr-pl')
                # run_btn = gr.Button('Generate Normals and Colors', variant='primary', interactive=True)
                run_btn = gr.Button('Reconstruct 3D model', variant='primary', interactive=True)
                gr.Markdown("<span style='color:red'> Reconstruction may cost several minutes. Check results in NeuS/exp/neus/scene-{current-time}/ </span>")
        
        with gr.Row():
            view_1 = gr.Image(interactive=True, height=240, show_label=False, show_download_button = True)
            view_2 = gr.Image(interactive=True, height=240, show_label=False, show_download_button = True)
            view_3 = gr.Image(interactive=True, height=240, show_label=False, show_download_button = True)
            view_4 = gr.Image(interactive=True, height=240, show_label=False, show_download_button = True)
            view_5 = gr.Image(interactive=True, height=240, show_label=False, show_download_button = True)
            view_6 = gr.Image(interactive=True, height=240, show_label=False, show_download_button = True)
        with gr.Row():
            normal_1 = gr.Image(interactive=False, height=240, show_label=False)
            normal_2 = gr.Image(interactive=False, height=240, show_label=False)
            normal_3 = gr.Image(interactive=False, height=240, show_label=False)
            normal_4 = gr.Image(interactive=False, height=240, show_label=False)
            normal_5 = gr.Image(interactive=False, height=240, show_label=False)
            normal_6 = gr.Image(interactive=False, height=240, show_label=False)
        with gr.Row():
            recon_btn = gr.Button('Reconstruct from 2D views', variant='primary', interactive=True)

        run_btn.click(
            fn=partial(preprocess, predictor), inputs=[input_image, input_processing], outputs=[processed_image_highres, processed_image], queue=True
        ).success(
            fn=partial(run_pipeline, pipeline, cfg),
            inputs=[processed_image_highres, scale_slider, steps_slider, seed, crop_size, output_processing, scale_processing ],
            outputs=[view_1, view_2, view_3, view_4, view_5, view_6, normal_1, normal_2, normal_3, normal_4, normal_5, normal_6],
        ).success(
            process_3d, inputs=[mode, data_dir, scale_slider, crop_size, scale_processing, recon_method], outputs=[obj_3d]
        )
        
        recon_btn.click(
            process_from_2dview, inputs=[mode, data_dir, scale_slider, crop_size, scale_processing, recon_method, view_1, view_2, view_3, view_4, view_5, view_6, normal_1, normal_2, normal_3, normal_4, normal_5, normal_6], outputs=[obj_3d]
        )

        demo.queue().launch(share=True, max_threads=80)


if __name__ == '__main__':
    fire.Fire(run_demo)
