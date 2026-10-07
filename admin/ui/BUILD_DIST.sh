#!/bin/bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# Build Vue 3 Admin Panel to admin/dist/
# Run this inside admin/ui/ directory:
#   cd admin/ui && bash BUILD_DIST.sh

set -e
echo "🐝 Phidipus SpiderHub — Building Vue 3 Admin Panel..."

if ! command -v npm &>/dev/null; then
    echo "❌ npm not found. Install Node.js: https://nodejs.org"
    exit 1
fi

# Install deps if needed
if [ ! -d node_modules ]; then
    echo "📦 Installing dependencies..."
    npm install
fi

# Build
echo "🔨 Building..."
npm run build

echo "✅ Build complete → admin/dist/"
echo "   Phidipus Admin Panel will auto-serve from dist/ on next restart"
