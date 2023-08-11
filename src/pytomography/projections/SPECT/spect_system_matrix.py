from __future__ import annotations
import torch
import pytomography
from pytomography.transforms import Transform
from pytomography.metadata import SPECTObjectMeta, SPECTImageMeta
from pytomography.priors import Prior
from pytomography.utils import rotate_detector_z, pad_object, unpad_object, pad_image, unpad_image
from ..system_matrix import SystemMatrix

class SPECTSystemMatrix(SystemMatrix):
    r"""System matrix for SPECT imaging. By default, this applies to parallel hole collimators, but appropriate use of `im2im_transforms` can allow this system matrix to also model converging/diverging collimator configurations as well.
    
    Args:
            obj2obj_transforms (Sequence[Transform]): Sequence of object mappings that occur before forward projection.
            im2im_transforms (Sequence[Transform]): Sequence of image mappings that occur after forward projection.
            object_meta (SPECTObjectMeta): SPECT Object metadata.
            image_meta (SPECTImageMeta): SPECT Image metadata.
            n_parallel (int): Number of projections to use in parallel when applying transforms. More parallel events may speed up reconstruction time, but also increases GPU usage. Defaults to 1.
    """
    def __init__(
        self,
        obj2obj_transforms: list[Transform],
        im2im_transforms: list[Transform],
        object_meta: SPECTObjectMeta,
        image_meta: SPECTImageMeta,
        n_parallel = 1,
    ) -> None:
        super(SPECTSystemMatrix, self).__init__(obj2obj_transforms, im2im_transforms, object_meta, image_meta)
        self.n_parallel = n_parallel
        
    def get_ang_idx_parallel(self, i, ang_idx, batch_size):
        ang_idx_parallel = ang_idx[i:i+self.n_parallel]
        ang_idx_parallel = torch.repeat_interleave(ang_idx_parallel, batch_size)

    def forward(
        self,
        object: torch.tensor,
        angle_subset: list[int] = None,
    ) -> torch.tensor:
        r"""Applies forward projection to ``object`` for a SPECT imaging system.

        Args:
            object (torch.tensor[batch_size, Lx, Ly, Lz]): The object to be forward projected
            angle_subset (list, optional): Only uses a subset of angles (i.e. only certain values of :math:`j` in formula above) when back projecting. Useful for ordered-subset reconstructions. Defaults to None, which assumes all angles are used.

        Returns:
            torch.tensor[batch_size, Ltheta, Lx, Lz]: Forward projected image where Ltheta is specified by `self.image_meta` and `angle_subset`.
        """
        N_angles = self.image_meta.num_projections
        object = object.to(pytomography.device)
        image = torch.zeros((object.shape[0],*self.image_meta.padded_shape)).to(pytomography.device)
        ang_idx = torch.arange(N_angles) if angle_subset is None else angle_subset
        for i in range(0, len(ang_idx), self.n_parallel):
            ang_idx_parallel = ang_idx[i:i+self.n_parallel]
            object_i = rotate_detector_z(pad_object(object.repeat(len(ang_idx_parallel),1,1,1)), self.image_meta.angles[ang_idx_parallel])
            for transform in self.obj2obj_transforms:
                object_i = transform.forward(object_i, ang_idx_parallel)
            if self.n_parallel==1:
                # Allows for batched inputs
                image[:,ang_idx_parallel] = object_i.sum(axis=1).unsqueeze(1)
            else:
                # Parallel projections
                image[:,ang_idx_parallel] = object_i.sum(axis=1)
        for transform in self.im2im_transforms:
            image = transform.forward(image)
        return unpad_image(image)
    
    def backward(
        self,
        image: torch.tensor,
        angle_subset: list | None = None,
        return_norm_constant: bool = False,
    ) -> torch.tensor:
        r"""Applies back projection to ``image`` for a SPECT imaging system.

        Args:
            image (torch.tensor[batch_size, Ltheta, Lr, Lz]): image which is to be back projected
            angle_subset (list, optional): Only uses a subset of angles (i.e. only certain values of :math:`j` in formula above) when back projecting. Useful for ordered-subset reconstructions. Defaults to None, which assumes all angles are used.
            return_norm_constant (bool): Whether or not to return :math:`1/\sum_j H_{ij}` along with back projection. Defaults to 'False'.

        Returns:
            torch.tensor[batch_size, Lr, Lr, Lz]: the object obtained from back projection.
        """
        # Box used to perform back projection
        boundary_box_bp = pad_object(torch.ones((1, *self.object_meta.shape)).to(pytomography.device), mode='back_project')
        # Pad image and norm_image (norm_image used to compute sum_j H_ij)
        norm_image = torch.ones(image.shape).to(pytomography.device)
        image = pad_image(image)
        norm_image = pad_image(norm_image)
        # First apply image transforms before back projecting
        for transform in self.im2im_transforms[::-1]:
            image, norm_image = transform.backward(image, norm_image)
        # Setup for back projection
        N_angles = self.image_meta.num_projections
        object = torch.zeros([image.shape[0], *self.object_meta.padded_shape]).to(pytomography.device)
        norm_constant = torch.zeros([image.shape[0], *self.object_meta.padded_shape]).to(pytomography.device)
        ang_idx = torch.arange(N_angles) if angle_subset is None else angle_subset
        for i in range(0, len(ang_idx), self.n_parallel):
            ang_idx_parallel = ang_idx[i:i+self.n_parallel]
            # Perform back projection
            #object_i = image[0,ang_idx_parallel].unsqueeze(1) * boundary_box_bp
            #norm_constant_i = norm_image[0,ang_idx_parallel].unsqueeze(1) * boundary_box_bp
            object_i = image[:,ang_idx_parallel].flatten(0,1).unsqueeze(1) * boundary_box_bp
            norm_constant_i = norm_image[:,ang_idx_parallel].flatten(0,1).unsqueeze(1) * boundary_box_bp
            # Apply object mappings
            for transform in self.obj2obj_transforms[::-1]:
                object_i, norm_constant_i = transform.backward(object_i, ang_idx_parallel, norm_constant=norm_constant_i)
            # Add to total
            if self.n_parallel==1:
                # Allows for batched inputs of greater than 1
                norm_constant += rotate_detector_z(norm_constant_i, self.image_meta.angles[ang_idx_parallel], negative=True)
                object += rotate_detector_z(object_i, self.image_meta.angles[ang_idx_parallel], negative=True)
            else:
                # Must have batch_size = 1
                norm_constant += rotate_detector_z(norm_constant_i, self.image_meta.angles[ang_idx_parallel], negative=True).sum(axis=0).unsqueeze(0)
                object += rotate_detector_z(object_i, self.image_meta.angles[ang_idx_parallel], negative=True).sum(axis=0).unsqueeze(0)
        # Unpad
        norm_constant = unpad_object(norm_constant)
        object = unpad_object(object)
        # Return
        if return_norm_constant:
            return object, norm_constant+pytomography.delta
        else:
            return object
        
        
class SPECTSystemMatrixMaskedSegments(SPECTSystemMatrix):
    r"""Update this
    
    Args:
            obj2obj_transforms (Sequence[Transform]): Sequence of object mappings that occur before forward projection.
            im2im_transforms (Sequence[Transform]): Sequence of image mappings that occur after forward projection.
            object_meta (SPECTObjectMeta): SPECT Object metadata.
            image_meta (SPECTImageMeta): SPECT Image metadata.
            masks (torch.Tensor): Masks corresponding to each segmented region.
    """
    def __init__(
        self,
        obj2obj_transforms: list[Transform],
        im2im_transforms: list[Transform],
        object_meta: SPECTObjectMeta,
        image_meta: SPECTImageMeta,
        masks: torch.Tensor
        
    ) -> None:
        super(SPECTSystemMatrixMaskedSegments, self).__init__(obj2obj_transforms, im2im_transforms, object_meta, image_meta)
        self.masks = masks.to(pytomography.device)

    def forward(
        self,
        activities: torch.Tensor,
        angle_subset: list[int] = None,
    ) -> torch.Tensor:
        r"""Implements forward projection :math:`HUa` on a vector of activities :math:`a` corresponding to `self.masks`.

        Args:
            activities (torch.tensor[batch_size, n_masks]): Activities in each mask region.
            angle_subset (list, optional): Only uses a subset of angles (i.e. only certain values of :math:`j` in formula above) when back projecting. Useful for ordered-subset reconstructions. Defaults to None, which assumes all angles are used.

        Returns:
            torch.tensor[batch_size, Ltheta, Lx, Lz]: Forward projected image where Ltheta is specified by `self.image_meta` and `angle_subset`.
        """
        object = 0
        activities = activities.reshape((*activities.shape, 1, 1, 1)).to(pytomography.device)
        object = (activities*self.masks).sum(axis=1)
        return super(SPECTSystemMatrixMaskedSegments, self).forward(object, angle_subset)
    
    def backward(
        self,
        image: torch.Tensor,
        angle_subset: list | None = None,
        prior: Prior | None = None,
        normalize: bool = False,
        return_norm_constant: bool = False,
    ) -> torch.Tensor:
        """Implements back projection :math:`U^T H^T g` on an image :math:`g`, returning a vector of activities for each mask region.

        Args:
            image (torch.tensor[batch_size, Ltheta, Lr, Lz]): image which is to be back projected
            angle_subset (list, optional): Only uses a subset of angles (i.e. only certain values of :math:`j` in formula above) when back projecting. Useful for ordered-subset reconstructions. Defaults to None, which assumes all angles are used.
            prior (Prior, optional): If included, modifes normalizing factor to :math:`\frac{1}{\sum_j H_{ij} + P_i}` where :math:`P_i` is given by the prior. Used, for example, during in MAP OSEM. Defaults to None.
            normalize (bool): Whether or not to divide result by :math:`\sum_j H_{ij}`
            return_norm_constant (bool): Whether or not to return :math:`1/\sum_j H_{ij}` along with back projection. Defaults to 'False'.

        Returns:
            torch.tensor[batch_size, n_masks]: the activities in each mask region.
        """
        object, norm_constant = super(SPECTSystemMatrixMaskedSegments, self).backward(image, angle_subset, prior, normalize=False, return_norm_constant = True, delta = pytomography.delta)
        activities = (object.unsqueeze(dim=1) * self.masks).sum(axis=(-1,-2,-3))
        norm_constant = (norm_constant.unsqueeze(dim=1) * self.masks).sum(axis=(-1,-2,-3))
        if normalize:
            activities = (activities+pytomography.delta)/(norm_constant + pytomography.delta)
        if return_norm_constant:
            return activities, norm_constant+pytomography.delta
        else:
            return activities