#!/bin/bash
export SYNC_BASE=/home/ubuntu/ankiserver/anki-sync-data
export SYNC_PORT=27701

# Load users from file
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  usernum=$((usernum+1))
  export SYNC_USER${usernum}="$line"
done < /home/ubuntu/ankiserver/anki-sync-users.txt

exec /home/ubuntu/ankiserver/syncserver/bin/python -m anki.syncserver
