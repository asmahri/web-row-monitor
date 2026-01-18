name: Vessel Monitor

on:
  schedule:
    - cron: '*/30 * * * *'       # Monitor mode (every 30 mins)
    - cron: '0 8 1 * *'          # Report mode (1st of month @ 08:00)
  workflow_dispatch:             # Manual trigger
    inputs:
      mode:
        description: 'Select run mode'
        required: true
        default: 'monitor'
        type: choice
        options:
          - monitor
          - report

# Prevent overlapping runs, but allow one to finish before starting the next
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
    
    steps:
      - name: ⬇️ Checkout Repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
          # token: ${{ secrets.GITHUB_TOKEN }} # Default is fine, but explicit is clear

      - name: 🐍 Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: 📦 Install Dependencies
        run: pip install requests

      - name: 🚀 Run Script with Correct Mode
        id: run_script
        env:
          VESSEL_STATE_DATA: ${{ secrets.VESSEL_STATE_DATA }}
          EMAIL_USER: ${{ secrets.EMAIL_USER }}
          EMAIL_PASS: ${{ secrets.EMAIL_PASS }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
          EMAIL_TO_COLLEAGUE: ${{ secrets.EMAIL_TO_COLLEAGUE }}
          EMAIL_ENABLED: "true"
        run: |
          # 1. Determine RUN_MODE based on trigger
          if [[ "${{ github.event_name }}" == "workflow_dispatch" ]]; then
            # Manual trigger: Use the input selected in UI
            RUN_MODE="${{ github.event.inputs.mode }}"
          elif [[ "${{ github.event.schedule }}" == "0 8 1 * *" ]]; then
            # Scheduled trigger: Check for the monthly report cron
            RUN_MODE="report"
          else
            # Default scheduled monitor run
            RUN_MODE="monitor"
          fi

          echo "🚀 Executing MODE: $RUN_MODE"
          
          # 2. Persist RUN_MODE to environment for subsequent steps
          echo "RUN_MODE=$RUN_MODE" >> $GITHUB_ENV
          
          # 3. Run the python script
          python monitor.py

      - name: 💾 Commit and Push State and History
        # Commit on success OR failure (unless cancelled), but only if files changed
        if: success() || failure()
        run: |
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git config --global user.name "github-actions[bot]"
          
          # Track files
          git add state.json history.json
          
          if git diff --staged --quiet; then
            echo "✅ No changes to data files"
          else
            # SANITY CHECK: Ensure state.json is valid JSON before committing
            # This prevents pushing a corrupted file if the script crashed mid-write
            if python -m json.tool state.json > /dev/null; then
              echo "✅ state.json is valid"
            else
              echo "❌ ERROR: state.json is corrupted! Aborting commit."
              exit 1
            fi

            TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
            if [[ "$RUN_MODE" == "report" ]]; then
              git commit -m "📊 Monthly report & History Archive - $TIMESTAMP [skip ci]"
            else
              git commit -m "🤖 Vessel state update - $TIMESTAMP [skip ci]"
            fi
            
            echo "🚀 Pushing changes..."
            # Retry logic for robustness
            MAX_RETRIES=3
            for i in $(seq 1 $MAX_RETRIES); do
              if git push origin main; then
                echo "✅ Data committed successfully"
                break
              else
                echo "⚠️ Push attempt $i failed, pulling and retrying..."
                sleep 2
                # Rebase to avoid conflicts
                git pull --rebase origin main || true
              fi
            done
          fi
