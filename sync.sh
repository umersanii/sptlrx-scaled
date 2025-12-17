#!/bin/bash

# Sync main.py to the local bin directory
TARGET="/home/sadasani/.local/bin/sptlrx-scaled"
SOURCE="main.py"

if [ ! -f "$SOURCE" ]; then
    echo "Error: $SOURCE not found in current directory."
    exit 1
fi

echo "Syncing $SOURCE to $TARGET..."
cp "$SOURCE" "$TARGET"
chmod +x "$TARGET"

echo "Done! sptlrx-scaled updated."
