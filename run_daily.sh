#!/bin/bash

# Define colors for premium terminal output
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}=====================================================================${NC}"
echo -e "${CYAN}🌟🚀 RUNNING DAILY MULTIBAGGER SCANNER & SOIC RANKER PIPELINE 🚀🌟${NC}"
echo -e "${CYAN}=====================================================================${NC}"

# Navigate to the workspace directory of the script
cd "$(dirname "$0")"

# Check if python virtual environment exists
if [ ! -d "venv" ]; then
    echo -e "${RED}❌ Python virtual environment 'venv' not found in $(pwd)!${NC}"
    echo -e "${YELLOW}Please create it using: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt${NC}"
    exit 1
fi

# Step 1: Run the Multibagger Scanner
echo -e "\n${YELLOW}🔄 Step 1/2: Fetching live data from Chartink & screening signals...${NC}"
venv/bin/python scanner.py

if [ $? -ne 0 ]; then
    echo -e "${RED}❌ Step 1 failed! scanner.py encountered an error.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Step 1 complete! Today's signals fetched and logged successfully.${NC}"

# Step 2: Run the SOIC Ranker & Parallel StockScans API Scraper
echo -e "\n${YELLOW}🔄 Step 2/2: Rebuilding premium Excel sheet & scoring overlaps...${NC}"
venv/bin/python monit_ranker.py

if [ $? -ne 0 ]; then
    echo -e "${RED}❌ Step 2 failed! monit_ranker.py encountered an error.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Step 2 complete! Workbook rebuilt successfully.${NC}"

# Find the latest workbook in outputs
LATEST_EXCEL=$(ls -t outputs/monit_chartink_ranking_*.xlsx 2>/dev/null | head -n 1)

# Step 3: Automatically commit and push local changes (delivery data cache and workbook) to GitHub
echo -e "\n${YELLOW}🔄 Step 3/3: Committing and pushing delivery cache & workbook to GitHub...${NC}"
git add logs/backtest.db outputs/
git commit -m "chore: local auto-sync delivery cache and ranking workbook [skip ci]"
git pull --rebase --autostash origin main
git push origin main
echo -e "${GREEN}✅ Step 3 complete! Local changes successfully synced to GitHub.${NC}"

echo -e "\n${GREEN}=====================================================================${NC}"
echo -e "${GREEN}🏆 SUCCESS! Daily pipeline finished execution flawlessly. 🏆${NC}"
echo -e "${GREEN}=====================================================================${NC}"
if [ ! -z "$LATEST_EXCEL" ]; then
    echo -e "${GREEN}📈 Premium Watchlist Saved: ${YELLOW}$LATEST_EXCEL${NC}"
else
    echo -e "${RED}⚠️ No output sheet found in the outputs/ directory!${NC}"
fi
echo -e "${CYAN}=====================================================================${NC}"
