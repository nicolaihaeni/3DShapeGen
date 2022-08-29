import os
import pymesh
import h5py
import numpy as np
from joblib import Parallel, delayed
import trimesh
from scipy.interpolate import RegularGridInterpolator
import time
import argparse
import json
import constant

from mesh_to_sdf import get_surface_point_cloud

parser = argparse.ArgumentParser()
parser.add_argument("--mesh_dir", type=str, default=".", help="Orginal mesh directory")
parser.add_argument(
    "--norm_mesh_dir", type=str, default=".", help="Directory to save normalized mesh"
)
parser.add_argument("--sdf_dir", type=str, default=".", help="Directory to save sdf")
parser.add_argument("--json_path", type=str, default=".", help="Path to json file")
parser.add_argument(
    "--mode",
    type=str,
    default=None,
    help="Generating mode (train, val, test). If None all 3 are generated",
)
parser.add_argument(
    "--categories",
    type=str,
    default="shapenet_55",
    help="Short-handed categories to generate ground-truth",
)

parser.add_argument(
    "--num_samples", type=int, default=150000, help="Number of sdf sampled"
)
parser.add_argument(
    "--bandwidth", type=float, default=0.1, help="Bandwidth of sampling"
)
parser.add_argument(
    "--res", type=int, default=256, help="Resolution of grid to sample sdf"
)
parser.add_argument("--expand_rate", type=float, default=1.2, help="Max value of x,y,z")
parser.add_argument("--iso_val", type=float, default=0.003, help="Iso surface value")
parser.add_argument(
    "--max_verts", type=int, default=16384, help="Maximum number of vertices"
)

parser.add_argument(
    "--ish5", type=bool, default=True, help="Whether to save in h5py type"
)
parser.add_argument(
    "--normalize", type=bool, default=True, help="Whether to normalize gt mesh"
)
parser.add_argument(
    "--skip_all_exist",
    type=bool,
    default=True,
    help="Whether to skip existing ground-truth",
)

parser.add_argument(
    "--ptcl", type=bool, default=True, help="Whether to generate pointcloud"
)
parser.add_argument(
    "--ptcl_save_dir", type=str, default=".", help="Where to save pointclouds"
)
parser.add_argument("--ptcl_size", type=int, default=100000, help="Size of pointcloud")
parser.add_argument(
    "--num_split", type=int, default=12, help="Number of threads to use"
)
args = parser.parse_args()


def get_sdf(sdf_file, sdf_res):
    """
    Retrieves sdf from .sdf file
    """
    intsize = 4
    floatsize = 8
    sdf = {"param": [], "value": []}
    with open(sdf_file, "rb") as f:
        try:
            bytes = f.read()
            ress = np.fromstring(bytes[: intsize * 3], dtype=np.int32)
            if -1 * ress[0] != sdf_res or ress[1] != sdf_res or ress[2] != sdf_res:
                raise Exception(sdf_file, "res not consistent with ", str(sdf_res))
            positions = np.fromstring(
                bytes[intsize * 3 : intsize * 3 + floatsize * 6], dtype=np.float64
            )
            # bottom left corner, x,y,z and top right corner, x, y, z
            sdf["param"] = [
                positions[0],
                positions[1],
                positions[2],
                positions[3],
                positions[4],
                positions[5],
            ]
            sdf["param"] = np.float32(sdf["param"])
            sdf["value"] = np.fromstring(
                bytes[intsize * 3 + floatsize * 6 :], dtype=np.float32
            )
            sdf["value"] = np.reshape(
                sdf["value"], (sdf_res + 1, sdf_res + 1, sdf_res + 1)
            )
        finally:
            f.close()
    return sdf


def sample_sdf(cat_id, num_sample, bandwidth, iso_val, sdf_dict, sdf_res):
    """
    Samples points closer to the surface
    """
    start = time.time()
    percentages = [
        [-1.1, -1.0 * bandwidth, int(num_sample * 0.1)],
        [-1.0 * bandwidth, -1.0 * bandwidth * 0.30, int(num_sample * 0.15)],
        [-1.0 * bandwidth * 0.30, 0, int(num_sample * 0.25)],
        [0, bandwidth * 0.30, int(num_sample * 0.25)],
        [bandwidth * 0.30, bandwidth, int(num_sample * 0.15)],
        [bandwidth, 1.1, int(num_sample * 0.1)],
    ]
    params = sdf_dict["param"]
    sdf_values = sdf_dict["value"].flatten()
    x = np.linspace(params[0], params[3], num=sdf_res + 1).astype(np.float32)
    y = np.linspace(params[1], params[4], num=sdf_res + 1).astype(np.float32)
    z = np.linspace(params[2], params[5], num=sdf_res + 1).astype(np.float32)
    dis = sdf_values - iso_val
    sdf_pt_val = np.zeros((0, 4), dtype=np.float32)
    for i in range(len(percentages)):
        ind = np.argwhere((dis >= percentages[i][0]) & (dis < percentages[i][1]))
        if len(ind) < percentages[i][2]:
            if i < len(percentages) - 1:
                percentages[i + 1][2] += percentages[i][2] - len(ind)
            percentages[i][2] = len(ind)
        if len(ind) == 0:
            print("len(ind) ==0 for cate i")
            continue
        choice = np.random.randint(len(ind), size=percentages[i][2])
        choosen_ind = ind[choice]
        x_ind = choosen_ind % (sdf_res + 1)
        y_ind = (choosen_ind // (sdf_res + 1)) % (sdf_res + 1)
        z_ind = choosen_ind // (sdf_res + 1) ** 2
        x_vals = x[x_ind]
        y_vals = y[y_ind]
        z_vals = z[z_ind]
        vals = sdf_values[choosen_ind]
        sdf_pt_val_bin = np.concatenate((x_vals, y_vals, z_vals, vals), axis=-1)
        sdf_pt_val = np.concatenate((sdf_pt_val, sdf_pt_val_bin), axis=0)
    return sdf_pt_val


def create_h5_sdf_pt(
    cat_id,
    h5_file,
    sdf_file,
    cube_obj_file,
    norm_obj_file,
    centroid,
    m,
    sdf_res,
    num_sample,
    bandwidth,
    iso_val,
    max_verts,
    normalize,
):
    """
    Creates h5 file for sdf
    args:
        cat_id: category id
        h5_file: path to save h5 file
        sdf_file: path to sdf file
        cube_obj_file: path to iso mesh
        norm_obj_file: path to normalized mesh
        centroid: center of bounding box of original mesh
        m: scale of original mesh
        sdf_res: grid resolution
    """
    bounding_radius = 1.1
    iso_mesh = trimesh.load(cube_obj_file)
    ptcld = get_surface_point_cloud(iso_mesh, bounding_radius=bounding_radius)
    surface_pts = ptcld.get_random_surface_points(count=250000)
    free_pts = np.random.uniform(-bounding_radius, bounding_radius, (250000, 3))

    surface_sdf, surface_normals = ptcld.get_sdf(surface_pts, return_gradients=True)
    free_pts_sdf = ptcld.get_sdf(free_pts)
    surface_data = np.concatenate(
        [surface_pts, surface_normals, surface_sdf[:, None]], axis=-1
    )
    free_data = np.concatenate([free_pts, free_pts_sdf[:, None]], axis=-1)

    # import open3d as o3d

    # pcd = o3d.geometry.PointCloud()
    # pcd.points = o3d.utility.Vector3dVector(surface_pts)
    # pcd.normals = o3d.utility.Vector3dVector(surface_normals)
    # colors = np.zeros_like(surface_pts)
    # colors[:, 0] = 1
    # pcd.colors = o3d.utility.Vector3dVector(colors)
    # o3d.visualization.draw_geometries([pcd])

    # free_pcd = o3d.geometry.PointCloud()
    # free_pcd.points = o3d.utility.Vector3dVector(free_pts)
    # colors = np.zeros_like(free_pts)
    # colors[:, 1] = 1
    # free_pcd.colors = o3d.utility.Vector3dVector(colors)
    # o3d.visualization.draw_geometries([free_pcd, pcd])

    f1 = h5py.File(h5_file, "w")
    f1.create_dataset(
        "free_pts",
        data=free_data,
        compression="gzip",
    )
    f1.create_dataset(
        "surface_pts",
        data=surface_data,
        compression="gzip",
    )
    f1.close()

    # sdf_dict = get_sdf(sdf_file, sdf_res)
    # ori_verts = np.asarray([0.0, 0.0, 0.0], dtype=np.float32).reshape((1, 3))
    # samplesdf = sample_sdf(cat_id, num_sample, bandwidth, iso_val, sdf_dict, sdf_res)
    # norm_params = np.concatenate((centroid, np.asarray([m]).astype(np.float32)))

    # # Append to sdf_file
    # with h5py.File(h5_file, "r") as hf:
    # K = hf["K"][:]
    # category = hf["category"][...]
    # image = hf["image"][:]
    # depth = hf["depth"][:]
    # instance_name = hf["instance_name"][...]
    # mask = hf["mask"][:]
    # normal = hf["normal"][:]
    # sdf = samplesdf
    # w2c = hf["w2c"][:]

    # with h5py.File(h5_file, "w") as out_file:
    # out_file.create_dataset("K", data=K, dtype="f")
    # out_file.create_dataset("category", data=category)
    # out_file.create_dataset("instance_name", data=instance_name)
    # out_file.create_dataset("image", data=image, compression="gzip", dtype="f")
    # out_file.create_dataset("depth", data=depth, compression="gzip", dtype="f")
    # out_file.create_dataset("normal", data=normal, compression="gzip", dtype="f")
    # out_file.create_dataset("mask", data=mask, compression="gzip", dtype="f")
    # out_file.create_dataset("sdf", data=sdf, compression="gzip", dtype="f")
    # out_file.create_dataset("w2c", data=w2c, compression="gzip", dtype="f")

    # f1 = h5py.File(h5_file, "w")
    # f1.create_dataset(
    # "pc_sdf_original",
    # data=ori_verts.astype(np.float32),
    # compression="gzip",
    # compression_opts=4,
    # )
    # f1.create_dataset(
    # "pc_sdf_sample",
    # data=samplesdf.astype(np.float32),
    # compression="gzip",
    # compression_opts=4,
    # )
    # f1.create_dataset(
    # "norm_params", data=norm_params, compression="gzip", compression_opts=4
    # )
    # f1.create_dataset(
    # "sdf_params", data=sdf_dict["param"], compression="gzip", compression_opts=4
    # )
    # f1.close()
    command_str = "rm -rf " + norm_obj_file
    os.system(command_str)
    command_str = "rm -rf " + sdf_file
    os.system(command_str)


def get_normalize_mesh(model_file, norm_mesh_sub_dir):
    """
    Normalizes meshes with center of bounding box
    args:
        model_file: path to original mesh
        norm_mesh_sub_dir: path to normalized mesh
    """
    try:
        mesh = trimesh.load(model_file)
    except Exception:
        return None, None, None
    mesh = trimesh.Trimesh(mesh.vertices, mesh.faces)
    bbox = mesh.bounding_box.bounds
    extents = mesh.extents
    # Compute location and scale
    loc = (bbox[0] + bbox[1]) / 2
    scale = (bbox[1] - bbox[0]).max()

    # Transform input mesh to center of bounding box and normalize to
    # unit cube
    try:
        mesh.apply_translation(-loc)
        mesh.apply_scale(1 / scale)
    except Exception:
        mesh.vertices = mesh.vertices - loc
        mesh.vertices = mesh.vertices * 1 / scale

    centroid = loc
    m = scale

    obj_file = os.path.join(norm_mesh_sub_dir, "pc_norm.obj")
    mesh.export(obj_file)
    return obj_file, centroid, m


def create_one_sdf(sdfcommand, res, expand_rate, sdf_file, obj_file, indx, g=0.0):
    """
    Creates sdf for one object
    args:
        sdfcommand: command that calls computeDistanceField library
        obj_file: path to normalized mesh
        indx: index of sdf file
        g: param of computeDistanceField, default is 0.0
    """
    command_str = (
        sdfcommand
        + " "
        + obj_file
        + " "
        + str(res)
        + " "
        + str(res)
        + " "
        + str(res)
        + " -s "
        + " -e "
        + str(expand_rate)
        + " -o "
        + str(indx)
        + ".dist -m 1"
    )
    if g > 0.0:
        command_str += " -g " + str(g)
    os.system(command_str)
    command_str2 = "mv " + str(indx) + ".dist " + sdf_file
    os.system(command_str2)


def create_sdf_obj(
    sdfcommand,
    marching_cube_command,
    cat_mesh_dir,
    cat_norm_mesh_dir,
    cat_sdf_dir,
    obj,
    res,
    iso_val,
    expand_rate,
    indx,
    ish5,
    normalize,
    num_sample,
    bandwidth,
    max_verts,
    cat_id,
    g,
    skip_all_exist,
):
    obj = obj.rstrip("\r\n")
    sdf_sub_dir = os.path.join(cat_sdf_dir, obj)
    norm_mesh_sub_dir = os.path.join(cat_norm_mesh_dir, obj)
    if not os.path.exists(sdf_sub_dir):
        os.makedirs(sdf_sub_dir)
    if not os.path.exists(norm_mesh_sub_dir):
        os.makedirs(norm_mesh_sub_dir)
    sdf_file = os.path.join(sdf_sub_dir, "isosurf.sdf")
    cube_obj_file = os.path.join(norm_mesh_sub_dir, "isosurf.obj")
    h5_file = os.path.join(sdf_sub_dir, os.path.basename(sdf_sub_dir) + ".h5")

    # if os.path.exists(h5_file):
    # with h5py.File(h5_file, "r") as in_file:
    # num_points = 0
    # if "sdf" in in_file.keys():
    # num_points = in_file["sdf"][:].shape[0]

    if ish5 and os.path.exists(h5_file) and skip_all_exist:
        print("skip existed: ", h5_file)
    elif not ish5 and os.path.exists(sdf_file):
        print("skip existed: ", sdf_file)
    else:
        model_file = os.path.join(cat_mesh_dir, obj, "models", "model_normalized.obj")
        try:
            if normalize:
                norm_obj_file, centroid, m = get_normalize_mesh(
                    model_file, norm_mesh_sub_dir
                )

            create_one_sdf(
                sdfcommand, res, expand_rate, sdf_file, norm_obj_file, indx, g=g
            )
            create_one_cube_obj(marching_cube_command, iso_val, sdf_file, cube_obj_file)
            # change to h5
            if ish5:
                create_h5_sdf_pt(
                    cat_id,
                    h5_file,
                    sdf_file,
                    cube_obj_file,
                    norm_obj_file,
                    centroid,
                    m,
                    res,
                    num_sample,
                    bandwidth,
                    iso_val,
                    max_verts,
                    normalize,
                )
        except Exception:
            print("Fail to process ", model_file)


def create_one_cube_obj(marching_cube_command, i, sdf_file, cube_obj_file):
    """
    Creates iso-mesh for one object
    args:
        marching_cube_command: command that calls computeMarchingCube library
        i: iso-surface value
        sdf_file: path to sdf file of the object
        cube_obj_file: path to iso-mesh of the object
    """
    command_str = (
        marching_cube_command + " " + sdf_file + " " + cube_obj_file + " -i " + str(i)
    )
    os.system(command_str)
    return cube_obj_file


def create_sdf(
    sdfcommand,
    marching_cube_command,
    num_sample,
    bandwidth,
    res,
    expand_rate,
    cats,
    iso_val,
    max_verts,
    ish5=True,
    normalize=True,
    g=0.00,
    skip_all_exist=False,
    mesh_dir=".",
    norm_mesh_dir=".",
    sdf_dir=".",
    json_path=".",
    mode=None,
):
    """
    This function creates sdf values and iso meshes.
    args:
        sdfcommand: calls computeDistanceField library
        marching_cube_command: calls computeMarchingCube library to compute
            iso meshes from distance field
        num_sample: number of points get sampled during training
        bandwidth: bandwidth for points sampling
        res: grid resolution
        expand_rate: max x,y,z for sdf range
        cats: list of categories
        iso_val: iso-surface value
        max_verts: max number of vertices of iso mesh
        ish5: whether to export to h5 format
        normalize: whether to normalize mesh
        g: sdf command param, default is 0.0
        skip_all_exist: whether to skip existing sdf files
        mesh_dir: directory of original mesh
        norm_mesh_dir: directory of normalized iso mesh
        sdf_dir: directory of sdf files
        json_path: path to json files containing categories and objects
        mode: 'train', 'val', 'test'
    """
    if not os.path.exists(sdf_dir):
        os.makedirs(sdf_dir)

    if mode == None:
        mode = ["train", "val", "test"]
    else:
        mode = [mode]

    start = 0
    categories = os.listdir(mesh_dir)
    categories = [c for c in categories if c.startswith("0") if c in cats]
    for cat_id in categories:
        cat_sdf_dir = os.path.join(sdf_dir, cat_id)
        if not os.path.exists(cat_sdf_dir):
            os.makedirs(cat_sdf_dir)
        cat_mesh_dir = os.path.join(mesh_dir, cat_id)
        cat_norm_mesh_dir = os.path.join(norm_mesh_dir, cat_id)
        for md in mode:
            with open(json_path, "r") as json_file:
                split = json.load(json_file)
                if cat_id in split[md]:
                    list_obj = split[md][cat_id]
                else:
                    continue

            repeat = len(list_obj)
            indx_lst = [i for i in range(start, start + repeat)]
            sdfcommand_lst = [sdfcommand for i in range(repeat)]
            marching_cube_command_lst = [marching_cube_command for i in range(repeat)]
            cat_mesh_dir_lst = [cat_mesh_dir for i in range(repeat)]
            cat_norm_mesh_dir_lst = [cat_norm_mesh_dir for i in range(repeat)]
            cat_sdf_dir_lst = [cat_sdf_dir for i in range(repeat)]
            res_lst = [res for i in range(repeat)]
            expand_rate_lst = [expand_rate for i in range(repeat)]
            normalize_lst = [normalize for i in range(repeat)]
            iso_val_lst = [iso_val for i in range(repeat)]
            ish5_lst = [ish5 for i in range(repeat)]
            num_sample_lst = [num_sample for i in range(repeat)]
            bandwidth_lst = [bandwidth for i in range(repeat)]
            max_verts_lst = [max_verts for i in range(repeat)]
            cat_id_lst = [cat_id for i in range(repeat)]
            g_lst = [g for i in range(repeat)]
            skip_all_exist_lst = [skip_all_exist for i in range(repeat)]
            skip_all_exist_lst = [skip_all_exist for i in range(repeat)]
            with Parallel(backend="multiprocessing") as parallel:
                parallel(
                    delayed(create_sdf_obj)(
                        sdfcommand,
                        marching_cube_command,
                        cat_mesh_dir,
                        cat_norm_mesh_dir,
                        cat_sdf_dir,
                        obj,
                        res,
                        iso_val,
                        expand_rate,
                        indx,
                        ish5,
                        norm,
                        num_sample,
                        bandwidth,
                        max_verts,
                        cat_id,
                        g,
                        skip_all_exist,
                    )
                    for sdfcommand, marching_cube_command, cat_mesh_dir, cat_norm_mesh_dir, cat_sdf_dir, obj, res, iso_val, expand_rate, indx, ish5, norm, num_sample, bandwidth, max_verts, cat_id, g, skip_all_exist in zip(
                        sdfcommand_lst,
                        marching_cube_command_lst,
                        cat_mesh_dir_lst,
                        cat_norm_mesh_dir_lst,
                        cat_sdf_dir_lst,
                        list_obj,
                        res_lst,
                        iso_val_lst,
                        expand_rate_lst,
                        indx_lst,
                        ish5_lst,
                        normalize_lst,
                        num_sample_lst,
                        bandwidth_lst,
                        max_verts_lst,
                        cat_id_lst,
                        g_lst,
                        skip_all_exist_lst,
                    )
                )
            start += repeat
    print("finish all")


if __name__ == "__main__":
    mesh_dir = args.mesh_dir
    norm_mesh_dir = args.norm_mesh_dir
    sdf_dir = args.sdf_dir
    json_path = args.json_path
    mode = args.mode
    ptcl = args.ptcl
    ptcl_save_dir = args.ptcl_save_dir
    ptcl_size = args.ptcl_size
    num_split = args.num_split
    categories = args.categories
    if categories == "shapenet_13":
        cats = constant.shapenet_13
    elif categories == "shapenet_42":
        cats = constant.shapenet_42
    elif categories == "shapenet_55":
        cats = constant.shapenet_55
    elif categories == "shapenet_plane":
        cats = ["02691156"]
    elif categories == "shapenet_cars":
        cats = constant.shapenet_car
    else:
        raise Exception("Please implement customed categories here")

    num_samples = args.num_samples
    bandwidth = args.bandwidth
    res = args.res
    expand_rate = args.expand_rate
    iso_val = args.iso_val
    max_verts = args.max_verts
    ish5 = args.ish5
    normalize = args.normalize

    create_sdf(
        "./isosurface/computeDistanceField",
        "./isosurface/computeMarchingCubes",
        num_samples,
        bandwidth,
        res,
        expand_rate,
        cats,
        iso_val,
        max_verts,
        ish5=ish5,
        normalize=normalize,
        g=0.00,
        skip_all_exist=True,
        mesh_dir=mesh_dir,
        norm_mesh_dir=norm_mesh_dir,
        sdf_dir=sdf_dir,
        json_path=json_path,
        mode=mode,
    )
    if ptcl:
        print("Generating pointcloud")
        os.system(
            "python ./gt_gen/generate_ptcld.py --mesh_dir=%s \
            --json_path=%s --save_dir=%s --pointcloud_size=%d \
            --num_split=%d --mode=%s"
            % (norm_mesh_dir, json_path, ptcl_save_dir, ptcl_size, num_split, mode)
        )
