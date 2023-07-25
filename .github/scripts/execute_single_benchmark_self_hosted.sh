#!/bin/bash

# Datasets are stored in /usr/local/gtsfm-data in order to reduce runtimes by not 
# redownloading each (very large) dataset every CI run. Any new datasets must be
# downloaded and stored in /usr/local/gtsfm-data before running this action.
DATASET_PREFIX=/usr/local/gtsfm-data

DATASET_NAME=$1
CONFIG_NAME=$2
MAX_FRAME_LOOKAHEAD=$3
IMAGE_EXTENSION=$4
LOADER_NAME=$5
MAX_RESOLUTION=$6
SHARE_INTRINSICS=$7

# Extract the data, configure arguments for runner.
if [ "$DATASET_NAME" == "skydio-501" ]; then
  IMAGES_DIR="skydio-crane-mast-501-images"
  COLMAP_FILES_DIRPATH="skydio-501-colmap-pseudo-gt"
elif [ "$DATASET_NAME" == "tanks-and-temples-barn-410" ]; then
  DATASET_ROOT="Tanks_and_Temples_Barn_410"
  SCENE_NAME="Barn"
fi

## Download the Tanks & Temples dataset. ################
# WGET_URL1=https://github.com/johnwlambert/gtsfm-datasets-mirror/releases/download/tanks-and-temples-barn/Tanks_and_Temples_Barn_410.zip
# ZIP_FNAME=Tanks_and_Temples_Barn_410.zip
# echo "Downloading ${ZIP_FNAME} with WGET"
# wget -O $DATASET_PREFIX/Tanks_and_Temples_Barn_410.zip $WGET_URL1
# unzip -qq $DATASET_PREFIX/$ZIP_FNAME -d $DATASET_PREFIX
# pwd $DATASET_PREFIX
# ls -ltrh $DATASET_PREFIX
# ls -ltrh $DATASET_PREFIX/$DATASET_ROOT
#########################################


echo "Config: ${CONFIG_NAME}, Loader: ${LOADER_NAME}"
echo "Max. Frame Lookahead: ${MAX_FRAME_LOOKAHEAD}, Image Extension: ${IMAGE_EXTENSION}, Max. Resolution: ${MAX_RESOLUTION}"
echo "Share intrinsics for all images? ${SHARE_INTRINSICS}"

# Setup the command line arg if intrinsics are to be shared
if [ "$SHARE_INTRINSICS" == "true" ]; then
  export SHARE_INTRINSICS_ARG="--share_intrinsics"
else
  export SHARE_INTRINSICS_ARG=""
fi

# Run GTSFM on the dataset.
if [ "$LOADER_NAME" == "olsson-loader" ]; then
  python gtsfm/runner/run_scene_optimizer_olssonloader.py \
    --dataset_root $DATASET_PREFIX/$DATASET_ROOT \
    --image_extension $IMAGE_EXTENSION \
    --config_name ${CONFIG_NAME}.yaml \
    --max_frame_lookahead $MAX_FRAME_LOOKAHEAD \
    --max_resolution ${MAX_RESOLUTION} \
    ${SHARE_INTRINSICS_ARG}

#     --correspondence_generator_config_name loftr.yaml \

elif [ "$LOADER_NAME" == "colmap-loader" ]; then
  python gtsfm/runner/run_scene_optimizer_colmaploader.py \
    --images_dir $DATASET_PREFIX/${IMAGES_DIR} \
    --colmap_files_dirpath $DATASET_PREFIX/$COLMAP_FILES_DIRPATH \
    --config_name ${CONFIG_NAME}.yaml \
    --max_frame_lookahead $MAX_FRAME_LOOKAHEAD \
    --max_resolution ${MAX_RESOLUTION} \
    ${SHARE_INTRINSICS_ARG} \
    --num_workers 1 \
    --mvs_off

elif [ "$LOADER_NAME" == "astrovision" ]; then
  python gtsfm/runner/run_scene_optimizer_astrovision.py \
    --data_dir $DATASET_PREFIX/$DATASET_ROOT \
    --config_name ${CONFIG_NAME}.yaml \
    --max_frame_lookahead $MAX_FRAME_LOOKAHEAD \
    --max_resolution ${MAX_RESOLUTION} \
    ${SHARE_INTRINSICS_ARG}

elif [ "$LOADER_NAME" == "tanks-and-temples" ]; then
  python gtsfm/runner/run_scene_optimizer_synthetic_tanks_and_temples.py \
    --config_name ${CONFIG_NAME}.yaml \
    --dataset_root $DATASET_ROOT \
    --scene_name $SCENE_NAME
fi
