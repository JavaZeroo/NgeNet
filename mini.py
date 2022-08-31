import os

data = '/mnt/data/0815metaPcds'

fils = os.listdir(data)
output = '/mnt/data/0815metaPcds_mini'
os.mkdir(output)
for file in fils:
    up_dir = os.path.join(data, file, 'up', '1.pcd')
    down_dir = os.path.join(data, file, 'down', '1.pcd')
    center_dir = os.path.join(data, file, 'center', '1.pcd')
    calib_dir = os.path.join(data, file, 'calib.json')
    
    file_out = os.path.join(output, file)
    os.mkdir(file_out)

    up_out = os.path.join(file_out, 'up.pcd')
    down_out = os.path.join(file_out, 'down.pcd')
    center_out = os.path.join(file_out, 'center.pcd')
    calib_out = os.path.join(file_out)

    os.system(f'cp {up_dir} {up_out}')
    os.system(f'cp {down_dir} {down_out}')
    os.system(f'cp {center_dir} {center_out}')
    os.system(f'cp {calib_dir} {calib_out}')