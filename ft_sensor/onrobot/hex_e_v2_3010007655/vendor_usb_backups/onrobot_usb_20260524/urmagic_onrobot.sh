#!/bin/bash

if [ -z $DISPLAY ]; then
  export DISPLAY=:0
fi

#file copy with detect
MOUNTPOINT="$1"

cd "$MOUNTPOINT"

. /etc/profile

function copy_config() {
    SOURCE="$MOUNTPOINT/OnRobot_UR_Config/*.*"
    DEST="/root/GUI"
    
    cp -f -u $SOURCE $DEST #copy only if not exists

    # Make sure data is written to the USB key
    sync
    sync
}

copy_config

cd "$MOUNTPOINT"

DISPLAY=:0 java onrobot

# pkill java