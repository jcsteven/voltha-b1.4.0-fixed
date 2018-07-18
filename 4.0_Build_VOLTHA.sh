#!/bin/bash
# -------------------------------------------------------
# Automatic preparation script for rtk_openwrt 
# JC Yu,     Novenber 26,2015
# -------------------------------------------------------
# IMPORTANT:
#   When use: './<this script file>  '
# -------------------------------------------------------
TODAY=`date +"%Y-%m%d-%H%M"`
:${PPWW:=`pwd`}
BLOG_DIR="Build-log"
BLOG_DIR_WK=${HOME}/${BLOG_DIR}
Record_File=${BLOG_DIR_WK}/voltha-b1.4.0-log.txt


[ -d $BLOG_DIR_WK ] || mkdir $BLOG_DIR_WK
s_time=$(date +%s)
echo "==============================================================================" |  tee -a $Record_File
echo "Start:ASFVOLT16-Build-VOLTHA:${TODAY}" | tee -a $Record_File



#cd ${VOLTHA_DIR}
. env.sh
REPOSITORY=voltha/  TAG=1.4.0 make fetch
REPOSITORY=voltha/  TAG=1.4.0 make install-protoc
REPOSITORY=voltha/  TAG=1.4.0 make build
#cd  ${PPWW}
  
e_time=$(date +%s)
elap_s=$((e_time-s_time))
ss=$((elap_s%60))
mm=$(((elap_s/60)%60))
hh=$((elap_s/3600))
echo "------------------------------------------------------------------------------" | tee -a $Record_File
echo "End  :ASFVOLT16-Build-VOLTHA:${TODAY}" | tee -a $Record_File
echo "Build total time: $hh:$mm:$ss" | tee -a $Record_File
echo "==============================================================================" | tee -a $Record_File







