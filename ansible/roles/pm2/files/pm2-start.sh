#!/bin/bash
export PM2_HOME=/home/ubuntu/.pm2
/usr/lib/node_modules/pm2/bin/pm2 resurrect
sleep 2
/usr/lib/node_modules/pm2/bin/pm2 list
