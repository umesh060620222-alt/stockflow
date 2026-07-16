#!/bin/bash
# Run this on the Oracle Cloud instance after SSH in

# 1. Install Python + git
sudo apt update && sudo apt install -y python3 python3-pip git screen

# 2. Clone your repo (replace with your actual repo URL)
git clone https://github.com/umesh060620222-alt/stockflow.git
cd stockflow

# 3. Install dependencies
pip3 install -r requirements.txt

# 4. Create kite_secrets.py with your credentials
# (copy from your local machine or paste directly)
cat > kite_secrets.py << 'EOF'
API_KEY    = "PASTE_YOUR_API_KEY_HERE"
API_SECRET = "PASTE_YOUR_API_SECRET_HERE"
EOF

# 5. Find the instance's public IP (add this to Kite whitelist)
curl -s https://api.ipify.org && echo ""

# 6. Run the server (screen keeps it alive after you close SSH)
screen -S stockflow -dm bash -c 'PORT=8000 python3 app.py'
echo "Server running. Check with: screen -r stockflow"
