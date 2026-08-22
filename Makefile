.PHONY: setup doctor demo run test eval clean
setup:   ; pip install -r requirements.txt && cp -n .env.example .env || true
doctor:  ; python -m reelpulse doctor
demo:    ; python -m reelpulse demo
run:     ; python -m reelpulse run
test:    ; pytest tests/ -q
eval:    ; python eval/harness.py
clean:   ; rm -rf data/reelpulse.db data/latest.json data/weeks docs/index.html
