import argparse
import os
import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as utils
from tqdm import tqdm
import torch.nn.functional as F
import math
import nibabel as nib
# from arguments import get_args
from libs.helpers import get_data, get_grad, img_infor, reshape_img, extract_slice, mask_img, norm_img
from models.base import Net
from models.vae_gau import Net_VAE
from models.vae_gmm import Net_GMM
import scipy.ndimage as ndimage
from libs.helpers import cart2sphere, sphere2cart
from libs.helpers import direction_average
import yaml
from pathlib import Path


def main(args):
    
    if args.config_file:
        config_file = args.config_file
    else:
        print("No config file specified. Using default arguments.")

    with open(config_file, "r") as f:
        train_params = yaml.full_load(f)

    # read the training parameters from config file:
    prior_std = train_params['model']['prior_std']
    model_ml = train_params['model']['name']
    nparams = train_params['model']['nparams']
    model_mri = train_params['model']['mri']
    activation = train_params['model']['activation']
    samples = train_params['model']['samples']
    k = train_params['model']['clusters']
    tau = train_params['model']['tau']
    mc_samples = train_params['model']['mc_samples']

    num_workers = train_params['train']['num_workers']    
    lr = train_params['train']['lr']
    epochs = train_params['train']['epochs_no']
    batch = train_params['train']['batch_size']
    alpha = train_params['train']['alpha']
    anneal_rate = train_params['train']['anneal_rate']
    warmup = train_params['train']['warmup']
    
    save_path = train_params['save_path']
    dataset = train_params['dataset']['name']
    seed = train_params['seed']
    device = train_params['device']
    data_path = train_params['dataset']['data_dir']
    
    if dataset == 'hpc':
        # Load diffusion data:
        directory = data_path
        bvals = np.loadtxt(directory + '/T1w/Diffusion/bvals')
        bvals *= 1e-03

        bvecs = np.loadtxt(directory + '/T1w/Diffusion/bvecs')
        bvecs = np.transpose(bvecs)

        grad = np.concatenate((bvecs, bvals[:, None]), axis=1)

        # The following will be put into data loader function later ===================================
        img = nib.load(directory + '/T1w/Diffusion/data.nii.gz')
        mask = nib.load(directory + '/T1w/Diffusion/nodif_brain_mask.nii.gz')
        img = img.get_fdata()
        mask = mask.get_fdata()

    elif dataset == 'placenta':
        # Load diffusion data:
        directory = data_path
        grad = np.loadtxt(directory + '/grad_echo_inv_TE4.txt')
        # print(np.shape(grad))
        bvals = grad[:, 3]
        bvals *= 1e-03

        # bvecs = np.loadtxt(directory + '/grad_echo_inv_TE4')
        bvecs = grad[:, 0:2]
        bvecs = np.transpose(bvecs)

        # grad = np.concatenate((bvecs, bvals[:, None]), axis=1)

        # The following will be put into data loader function later ===================================
        img = nib.load(directory + '/chip0244/chip0244_20_20_3401_T2MEdiff_moco_abs.all4e.nii.gz')
        mask = nib.load(directory + '/chip0244/chip0244_20_20_3401_T2MEdiff_moco_abs.e01_placenta_and_uterine_wall_mask_pjs.nii.gz')
        img = img.get_fdata() + 1e-8
        mask = mask.get_fdata()
        # img, grad = direction_average(img, grad)

    else:
        raise NotImplementedError

    nvoxtotal = np.prod(np.shape(img)[0:3])
    nvol = np.shape(img)[3]
    imgvox = np.reshape(img, (nvoxtotal, nvol))

    masktmp = np.zeros(np.shape(mask))
    # TODO: remove the slice_index later and make a new data loader that returns a batch of slices:
    slice_index = 70
    masktmp[:, :, slice_index] = mask[:, :, slice_index]
    mask = masktmp
    maskvox = np.reshape(mask, (nvoxtotal))

    imgvoxtofit = imgvox[maskvox == 1]

    normvol = np.where(bvals == min(bvals))

    imgvoxtofitnorm = imgvoxtofit / (np.tile(np.mean(imgvoxtofit[:, normvol], axis=2), (1, nvol)))
    # =============================================================================================

    b_values_no0 = torch.FloatTensor(bvals)
    gradient_directions_no0 = torch.FloatTensor(bvecs)

    if model_ml == 'mlp':
        if dataset == 'placenta':
            net = Net(gradient_directions_no0, b_values_no0, grad, nparams, model_mri)
        elif dataset == 'hpc':
            net = Net(gradient_directions_no0, b_values_no0, grad, nparams, model_mri)
        else:
            raise NotImplementedError

        model_save_path = args.save_path + '/models_' + dataset + '/' + model_ml + '/' + model_mri
        Path(model_save_path).mkdir(parents=True, exist_ok=True)

        model_name = model_ml + \
                     '_par_' + str(nparams) + \
                     '_mri_' + str(model_mri) + \
                     '_lr_' + str(lr) + \
                     '_epoch_' + str(epochs)

        model_save_path = model_save_path + '/' + model_name
        Path(model_save_path).mkdir(parents=True, exist_ok=True)

    elif model_ml == 'gaussian':
        if dataset == 'placenta':
            net = Net_VAE(gradient_directions_no0=gradient_directions_no0,
                          b_values_no0=b_values_no0,
                          grad=grad,
                          act=activation,
                          nparams=nparams,
                          samples=samples,
                          mri_model=model_mri,
                          prior_std=prior_std
                          )

        elif dataset == 'hpc':
            net = Net_VAE(gradient_directions_no0=gradient_directions_no0,
                          b_values_no0=b_values_no0,
                          grad=grad,
                          act=activation,
                          nparams=nparams,
                          samples=samples,
                          mri_model=model_mri,
                          prior_std=prior_std)

        else:
            raise NotImplementedError

        model_save_path = save_path + '/models_' + dataset + '/' + model_ml
        Path(model_save_path).mkdir(parents=True, exist_ok=True)

        model_name = model_ml + \
                     '_dim_' + str(samples) + \
                     '_par_' + str(nparams) + \
                     '_mri_' + str(model_mri) + \
                     '_std_' + str(prior_std) + \
                     '_lr_' + str(lr) + \
                     '_epoch_' + str(epochs) + \
                     '_alpha_' + str(alpha) + \
                     '_anneal_' + str(anneal_rate) + \
                     '_act_' + str(activation) + \
                     '_warm_' + str(warmup)

        model_save_path = model_save_path + '/' + model_name
        Path(model_save_path).mkdir(parents=True, exist_ok=True)

    elif model_ml == 'gmm':
        if dataset == 'placenta':
            net = Net_GMM(gradient_directions_no0=gradient_directions_no0,
                          b_values_no0=b_values_no0,
                          grad=grad,
                          act=activation,
                          k=k,
                          tau=tau,
                          nparams=nparams,
                          samples=samples,
                          mri_model=model_mri,
                          prior_std=prior_std
                          )

        elif dataset == 'hpc':
            net = Net_GMM(gradient_directions_no0=gradient_directions_no0,
                          b_values_no0=b_values_no0,
                          grad=grad,
                          k=k,
                          tau=tau,
                          act=activation,
                          nparams=nparams,
                          samples=samples,
                          mri_model=model_mri,
                          prior_std=prior_std)

        else:
            raise NotImplementedError

        model_save_path = save_path + '/models_' + dataset + '/' + model_ml
        Path(model_save_path).mkdir(parents=True, exist_ok=True)

        model_name = model_ml + \
                     '_dim_' + str(samples) + \
                     '_par_' + str(nparams) + \
                     '_k_' + str(k) + \
                     '_mri_' + str(model_mri) + \
                     '_std_' + str(prior_std) + \
                     '_lr_' + str(lr) + \
                     '_tau_' + str(tau) + \
                     '_epoch_' + str(epochs) + \
                     '_alpha_' + str(alpha) + \
                     '_anneal_' + str(anneal_rate) + \
                     '_act_' + str(activation) + \
                     '_warm_' + str(warmup)

        model_save_path = model_save_path + '/' + model_name
        Path(model_save_path).mkdir(parents=True, exist_ok=True)

    else:
        raise NotImplementedError

    criterion = nn.MSELoss(reduction='mean')
    optimizer = optim.Adam(net.parameters(), lr=lr)

    trainloader = utils.DataLoader(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)),
                                    batch_size=128,
                                    shuffle=True,
                                    num_workers=2,
                                    drop_last=True)

    # num_bad_epochs = 0
    # best = 1e-16

    best_l2 = 100.

    for epoch in range(epochs):
        print("-----------------------------------------------------------------")
        print("Epoch: {}".format(epoch))
        net.train()
        running_loss = 0.

        running_loss_l2 = 0.

        running_loss_kl = 0.
        running_loss_kl_scaled = 0.

        running_loss_kl2 = 0.
        running_loss_kl_scaled2 = 0.

        for i, X_batch in enumerate(tqdm(trainloader), 0):

            optimizer.zero_grad()
            if model_ml == 'mlp':
                outputs = net(X_batch)
                loss = criterion(outputs['signal'], X_batch)

            elif model_ml == 'gaussian':
                # X_pred, D_par_pred, D_iso_pred, mu_pred, Fp_pred, mu, log_var, consistency_loss = net(X_batch)
                outputs = net(X_batch)
                loss = criterion(outputs['signal'], X_batch)
                running_loss_l2 += loss.item()
                kl_loss = torch.mean(-0.5 * torch.sum(1 + outputs['log_var'] - outputs['mu'] ** 2 - outputs['log_var'].exp(), dim=1), dim=0)

                # annealing schedule of kl loss
                if epoch < warmup*epochs:
                    alpha = 0.0
                else:
                    alpha = anneal_rate*(epoch - warmup*epochs)
                    alpha = min(alpha, alpha)

                loss += kl_loss*alpha

                running_loss_kl += kl_loss.item()
                running_loss_kl_scaled += alpha*kl_loss.item()

            elif model_ml == 'gmm':
                # X_pred, D_par_pred, D_iso_pred, mu_pred, Fp_pred, mu, log_var, consistency_loss = net(X_batch)
                outputs = net(X_batch)
                loss = criterion(outputs['signal'], X_batch)
                running_loss_l2 += loss.item()
                kl_loss = torch.mean(-0.5 * torch.sum(1 + outputs['log_var'] - outputs['mu'] ** 2 - outputs['log_var'].exp(), dim=1), dim=0)

                qy = F.softmax(outputs['y_logits'], dim=-1)
                log_q = F.log_softmax(outputs['y_logits'], dim=-1)
                target = torch.ones(1)*(1 / args.model.k)
                kl_loss2 = -torch.mean(torch.sum(qy * (log_q - torch.log(target)), dim=-1))

                # annealing schedule of kl loss
                if epoch < warmup*epochs:
                    alpha_ = 0.0
                else:
                    alpha_ = anneal_rate*(epoch - warmup*epochs)
                    alpha_ = min(alpha, alpha_)

                loss += kl_loss*alpha_ + kl_loss2*alpha_

                running_loss_kl += kl_loss.item()
                running_loss_kl_scaled += alpha_*kl_loss.item()

                running_loss_kl2 += kl_loss2.item()
                running_loss_kl_scaled2 += alpha_*kl_loss2.item()

            else:
                raise NotImplementedError

            loss.backward()
            optimizer.step()
            running_loss += loss.item()

            if running_loss_l2 < best_l2:
                save_model_name_full = model_save_path + '/' + model_name + '_best.pt'
                torch.save(net, save_model_name_full)
                best_l2 = running_loss_l2

        if model_ml == 'mlp':
            print("Loss: {}".format(running_loss))
        elif model_ml == 'gaussian':
            print("L2 Loss: {}".format(running_loss_l2))
            print("KL Loss: {}".format(running_loss_kl))
            print("Scaled KL Loss: {}".format(running_loss_kl_scaled))
        elif model_ml == 'gmm':
            print("L2 Loss: {}".format(running_loss_l2))
            print("KL Loss: {}".format(running_loss_kl))
            print("Scaled KL Loss: {}".format(running_loss_kl_scaled))
            print("KL Loss 2: {}".format(running_loss_kl2))
            print("Scaled KL Loss 2: {}".format(running_loss_kl_scaled2))
        else:
            raise NotImplementedError

    print("Done")

    save_model_name_full = model_save_path + '/' + model_name + '.pt'
    torch.save(net, save_model_name_full)

    if model_mri == 'ball_stick':
        # X_real_pred = X_real_pred.numpy()
        # D_par = D_par.numpy()
        # D_iso = D_iso.numpy()
        # mu_cart = mu_cart.numpy()
        # Fp = Fp.numpy()

        net.eval()

        fig, ax = plt.subplots(6, mc_samples, figsize=(30, 30))

        for i in range(mc_samples):
            with torch.no_grad():
                outputs = net(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)))

            D_par = outputs['d_par'].numpy()
            D_iso = outputs['d_iso'].numpy()
            mu_cart = outputs['mu_cart'].numpy()
            Fp = outputs['fp'].numpy()

            mu_cart_transposed = mu_cart.transpose()
            mu_vals = cart2sphere(mu_cart_transposed)
            theta = mu_vals[:, 0]
            phi = mu_vals[:, 1]

            D_par_vox = np.zeros(np.shape(maskvox))
            D_par_vox[maskvox == 1] = np.squeeze(D_par[:])
            D_par_map = ndimage.rotate(np.reshape(D_par_vox, np.shape(mask)), 90, reshape=False)

            D_iso_vox = np.zeros(np.shape(maskvox))
            D_iso_vox[maskvox == 1] = np.squeeze(D_iso[:])
            D_iso_map = ndimage.rotate(np.reshape(D_iso_vox, np.shape(mask)), 90, reshape=False)

            theta_vox = np.zeros(np.shape(maskvox))
            theta_vox[maskvox == 1] = np.squeeze(theta[:])
            theta_map = ndimage.rotate(np.reshape(theta_vox, np.shape(mask)), 90, reshape=False)

            phi_vox = np.zeros(np.shape(maskvox))
            phi_vox[maskvox == 1] = np.squeeze(phi[:])
            phi_map = ndimage.rotate(np.reshape(phi_vox, np.shape(mask)), 90, reshape=False)

            Fp_vox = np.zeros(np.shape(maskvox))
            Fp_vox[maskvox == 1] = np.squeeze(Fp[:])
            Fp_map = ndimage.rotate(np.reshape(Fp_vox, np.shape(mask)), 90, reshape=False)

            mu_cart_vox = np.zeros((np.shape(maskvox)[0], 3))
            mu_cart_vox[maskvox == 1, :] = np.transpose(mu_cart[:])
            mu_cart_map = ndimage.rotate(np.reshape(mu_cart_vox, np.append(np.shape(mask), 3)), 90, reshape=False)

            zslice = slice_index

            plt0 = ax[0, i].imshow(D_par_map[:, :, zslice], vmin=0., vmax=3.)
            D_par_map = nib.Nifti1Image(D_par_map, affine=np.eye(4))
            nib.save(D_par_map, model_save_path + '/volume_' + 'D_par_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[0, i])
            ax[0, i].xaxis.set_ticklabels([])
            ax[0, i].set_title('stick parallel diffusivity ($\mu$m$^2$/ms)' + ' mc sample ' + str(i))
            ax[0, i].axis('off')

            plt0 = ax[1, i].imshow(D_iso_map[:, :, zslice], vmin=0., vmax=3.)
            D_iso_map = nib.Nifti1Image(D_iso_map, affine=np.eye(4))
            nib.save(D_iso_map, model_save_path + '/volume_' + 'D_iso_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[1, i])
            ax[1, i].set_title('ball isotropic diffusivity ($\mu$m$^2$/ms)' + ' mc sample ' + str(i))
            ax[1, i].axis('off')

            plt0 = ax[2, i].imshow(theta_map[:, :, zslice])
            plt.colorbar(plt0, ax=ax[2, i])
            theta_map = nib.Nifti1Image(theta_map, affine=np.eye(4))
            nib.save(theta_map, model_save_path + '/volume_' + 'theta_' + ' mc sample ' + str(i) + '.nii.gz')
            ax[2, i].set_title('theta' + ' mc sample ' + str(i))
            ax[2, i].axis('off')

            plt0 = ax[3, i].imshow(phi_map[:, :, zslice])
            phi_map = nib.Nifti1Image(phi_map, affine=np.eye(4))
            nib.save(phi_map, model_save_path + '/volume_' + 'phi_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[3, i])
            ax[3, i].set_title('phi' + ' mc sample ' + str(i))
            ax[3, i].axis('off')

            plt0 = ax[4, i].imshow(1 - Fp_map[:, :, zslice], vmin=0., vmax=1.)
            Fp_map = nib.Nifti1Image(Fp_map, affine=np.eye(4))
            nib.save(Fp_map, model_save_path + '/volume_' + 'Fp_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[4, i])
            ax[4, i].set_title('stick volume fraction' + ' mc sample ' + str(i))
            ax[4, i].axis('off')

            plt0 = ax[5, i].imshow(mu_cart_map[:, :, zslice, :])
            mu_cart_map = nib.Nifti1Image(mu_cart_map, affine=np.eye(4))
            nib.save(mu_cart_map, model_save_path + '/volume_' + 'mu_cart_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[5, i])
            ax[5, i].set_title('Color stick volume fraction' + ' mc sample ' + str(i))
            ax[5, i].axis('off')

    # elif args.model.mri == 't2adc':
    #
    #     mc_samples = args.mc_samples
    #     T2s = []
    #     Ds = []
    #
    #     net.eval()
    #
    #     for i in range(mc_samples):
    #
    #         with torch.no_grad():
    #             if args.model.name == 'mlp':
    #                 outputs = net(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)))
    #                 # X_real_pred, D_par, D_iso, mu_cart, Fp = net(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)))
    #             elif args.model.name == 'gaussian':
    #                 outputs = net(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)))
    #                 # X_real_pred, D_par, D_iso, mu_cart, Fp, mu, log_var, _ = net(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)))
    #             else:
    #                 raise NotImplementedError
    #
    #         T = outputs['t2'].numpy()
    #         D = outputs['d'].numpy()
    #         T2s.append(T)
    #         Ds.append(D)
    #
    #         # mu_cart_transposed = mu_cart.transpose()
    #         # mu_vals = cart2sphere(mu_cart_transposed)
    #         # theta = mu_vals[:, 0]
    #         # phi = mu_vals[:, 1]
    #
    #     fig, ax = plt.subplots(2, mc_samples, figsize=(20, 20))
    #
    #     for i in range(mc_samples):
    #         D_par = np.zeros(np.shape(maskvox))
    #         D_par[maskvox == 1] = np.squeeze(Ds[i][:])
    #         D_map = ndimage.rotate(np.reshape(D_par, np.shape(mask)), 90, reshape=False)
    #
    #         T2 = np.zeros(np.shape(maskvox))
    #         T2[maskvox == 1] = np.squeeze(T2s[i][:])
    #         T2 = ndimage.rotate(np.reshape(T2, np.shape(mask)), 90, reshape=False)
    #
    #         zslice = args.slice_index
    #
    #         plt0 = ax[0, i].imshow(D_map[:, :, zslice], vmin=0, vmax=3)
    #         plt.colorbar(plt0, ax=ax[0, i])
    #         ax[0, i].xaxis.set_ticklabels([])
    #         ax[0, i].set_title('D map' + ' mc sample ' + str(i))
    #         ax[0, i].axis('off')
    #
    #         plt0 = ax[1, i].imshow(T2[:, :, zslice], cmap='hot', vmin=0, vmax=2.0)
    #         plt.colorbar(plt0, ax=ax[1, i])
    #         ax[1, i].set_title('T2' + ' mc sample ' + str(i))
    #         ax[1, i].axis('off')

    elif model_mri == 'msdki':

        # mc_samples = args.mc_samples
        Ks = []
        Ds = []

        net.eval()

        for i in range(mc_samples):

            with torch.no_grad():
                outputs = net(torch.from_numpy(imgvoxtofitnorm.astype(np.float32)))

            K = outputs['k'].numpy()
            D = outputs['d'].numpy()
            Ks.append(K)
            Ds.append(D)

            # mu_cart_transposed = mu_cart.transpose()
            # mu_vals = cart2sphere(mu_cart_transposed)
            # theta = mu_vals[:, 0]
            # phi = mu_vals[:, 1]

        fig, ax = plt.subplots(2, mc_samples, figsize=(20, 20))

        for i in range(mc_samples):
            D_par = np.zeros(np.shape(maskvox))
            D_par[maskvox == 1] = np.squeeze(Ds[i][:])
            D_map = ndimage.rotate(np.reshape(D_par, np.shape(mask)), 90, reshape=False)

            k = np.zeros(np.shape(maskvox))
            k[maskvox == 1] = np.squeeze(Ks[i][:])
            k = ndimage.rotate(np.reshape(k, np.shape(mask)), 90, reshape=False)

            zslice = args.slice_index

            plt0 = ax[0, i].imshow(D_map[:, :, zslice], vmin=0, vmax=3)
            D_map_nii = nib.Nifti1Image(D_map, affine=np.eye(4))
            nib.save(D_map_nii, model_save_path + '/volume_'+'D_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[0, i])
            ax[0, i].xaxis.set_ticklabels([])
            ax[0, i].set_title('D' + ' mc sample ' + str(i))
            ax[0, i].axis('off')

            plt0 = ax[1, i].imshow(k[:, :, zslice], cmap='hot', vmin=0, vmax=2)
            K_map_nii = nib.Nifti1Image(k, affine=np.eye(4))
            nib.save(K_map_nii, model_save_path + '/volume_' + 'K_' + ' mc sample ' + str(i) + '.nii.gz')
            plt.colorbar(plt0, ax=ax[1, i])
            ax[1, i].set_title('K' + ' mc sample ' + str(i))
            ax[1, i].axis('off')

    else:
        raise NotImplementedError

    plt.tight_layout()
    plt.subplots_adjust(top=0.85)
    fig_save_name = model_save_path + '/summary.png'
    plt.savefig(fig_save_name, bbox_inches='tight')

    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train a simulated MRI model with PyTorch.")
    parser.add_argument(
        "--config-file",
        type=str,
        default=None,
        help="Path to config file to use for training",
    )
    args = parser.parse_args()
    main(args)

