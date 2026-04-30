from utils import *
from polrad import *
from ftp import *

from matplotlib import pyplot as plt
import geopandas as gpd
from itertools import product

#loading .env variables
from dotenv import load_dotenv
import os

load_dotenv(override=True)

PROJECT_PATH = os.getenv('PROJECT_PATH')
LOCAL_DIR     = Path(os.getenv("PROJECT_PATH"), 'img','polrad')
REMOTE_DIR    = os.getenv("FTP_REMOTE_IMG_DIR", "/public_html/img")



polrad_folder = os.path.join(PROJECT_PATH, 'data', 'polrad')
polrad_img_folder = os.path.join(PROJECT_PATH, 'img', 'polrad')

gdf_wojewodztwa = gpd.read_file(os.path.join(PROJECT_PATH, 'data', 'shapefiles', 'A01_Granice_wojewodztw.shp'))
gdf_powiaty = gpd.read_file(os.path.join(PROJECT_PATH, 'data', 'shapefiles', 'A02_Granice_powiatow.shp'))
gdf_shp_1 =  gpd.read_file(os.path.join(PROJECT_PATH, 'data', 'shapefiles', 'gadm41_POL.gpkg'), layer='ADM_ADM_1')
gdf_shp_2 =  gpd.read_file(os.path.join(PROJECT_PATH, 'data', 'shapefiles', 'gadm41_POL.gpkg'), layer='ADM_ADM_2')

output_projection = "EPSG:4326"

gdf_powiaty_map = gdf_powiaty.to_crs(output_projection)
gdf_wojewodztwa_map = gdf_wojewodztwa.to_crs(output_projection)
gdf_shp_1 = gdf_shp_1.to_crs(output_projection)
gdf_shp_2 = gdf_shp_2.to_crs(output_projection)

def main():
    clean_folder(polrad_folder)
    
    paths = ['/Oper/Polrad/Produkty/HVD/HVD_COMPO_CMAX_250.comp.cmax']

    radars = ['gdy','gsa','leg','pas','poz','ram','rze','swi','urz']
    products = ['125.cappi','200.dpsri', '250.max']

    #iter zip: kombinacja radarów i produktów
    for r, p in product(radars, products):
        path = f'/Oper/Polrad/Produkty/HVD/HVD_{r}_{p}'
        paths.append(path)

    #print(paths)

    for path in paths:
        df_files = get_list_of_files(path)
        df_files_to_download = df_files.tail(3)

        for idx, row in df_files_to_download.iterrows():

            output_filename = polrad_folder + '/' + row['filename']
            download_file(row['url'], output_filename)

            f = h5py.File(output_filename, 'r')
            f.close()


            radar_file = decode_h5_file(output_filename)

            if 'COMPO' not in path:
                region_prefix = ['RADAR']
            else:
                #województwa z shp
                region_prefix = gdf_shp_1['CC_1'].to_list()


            extent = None

            for region in region_prefix:
                image_filename = polrad_img_folder + '/' + \
                region + radar_file['system'] + '_' + radar_file['product'] + '_' + radar_file['quantity'] + '_' + row['filename'].replace('.h5', '') + '.png'
                
                if region != 'RADAR':
                    #get extent of region
                    gdf_region = gdf_shp_1[gdf_shp_1['CC_1'] == region]
                    minx, miny, maxx, maxy = gdf_region.total_bounds
                    print(f"Region: {region}, Extent: {minx}, {miny}, {maxx}, {maxy}")
                    extent = [minx, miny, maxx, maxy]

                plot_image(radar_file, image_filename, gdf_shp_1, gdf_shp_2, extent=extent)

                print(REMOTE_DIR)
                transfer_files(LOCAL_DIR, REMOTE_DIR, recursive=True)
                clean_folder(polrad_img_folder)

if __name__ == "__main__":
    main()