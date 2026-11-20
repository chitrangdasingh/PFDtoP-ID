# How to run and deploy

## A. Run on your own computer
1. Install **Python 3.11 or newer** (tick "Add python.exe to PATH") and **Graphviz** from graphviz.org (tick "Add Graphviz to PATH").
2. Open a terminal in this folder and run:

       python -m venv .venv
       .venv\Scripts\activate            (Mac/Linux: source .venv/bin/activate)
       pip install -r requirements.txt
       streamlit run app.py

3. The app opens at http://localhost:8501. Stop it with Ctrl+C.

## B. Put the folder on GitHub
Create an empty repository on github.com first (no README). Make it **private** if the plant data is confidential.

With git:

    git init
    git add .
    git commit -m "PFD to P&ID dashboard"
    git branch -M main
    git remote add origin https://github.com/<your-user>/<your-repo>.git
    git push -u origin main

Without git: on the repository page choose "Add file > Upload files", drag in everything from this folder, and commit.
Keep the folder structure (`data/`, `symbols/`, `models/`, `.streamlit/`). All `.py` files in the top folder are needed,
including `workflow.py`, `svg2dxf.py`, `kg_query.py`, `bom.py`, `copilot.py`, `copilot_nlu.py`, `copilot_kb.py`, `engineering.py`,
`balance.py`, `aspen_link.py`, `twin.py`, `gaps.py` and `fetch_pid2graph.py`, and the `models/` and `docs/` folders.
Corrections, remarks, BOM prices and the tag register are saved as CSV files in `data/`. On Streamlit Cloud these last until the app
restarts: download them, or commit them to GitHub, to keep them for good.

## C. Deploy on Streamlit Community Cloud (gives a shareable link)
1. Go to https://share.streamlit.io and sign in with GitHub.
2. "Create app" > pick the repository, branch `main`, main file path `app.py`.
3. Optional, for free-form questions and PFD import: Advanced settings > Secrets, paste

       ANTHROPIC_API_KEY = "your-key"

4. Deploy. `requirements.txt` installs the Python packages; `packages.txt` installs Graphviz.
5. Share the app URL. For a private repository, add viewers under the app's Settings > Sharing.

## C1. Aspen live link and PID2Graph (on your own PC, optional)
- Live link: on a Windows PC with Aspen Plus, `pip install pywin32`, then `python aspen_link.py --bkp "C:\path\model.bkp" --case aspen_10tpd --run`.
  Commit the new `data/aspen_10tpd_stream_results.csv`; the app shows what changed in the Aspen link tab.
- PID2Graph pre-training: `python fetch_pid2graph.py` downloads the public dataset from Zenodo and retrains `models/pid_model.*`. Commit the new model files.

## C2. Retrain the AI model (optional)
The repository ships a trained model (`models/pid_model.npz`, NumPy weights, no PyTorch needed). To retrain, for example after
adding reviewed P&IDs as new cases, run on your computer and commit the new files in `models/`:

    python train_pid_model.py --plants 1500

Add your own synthetic P&ID graphs in the PID2Graph GraphML format to `data/synthetic_pid/` and they are used for pre-training.

## C3. Check the DEXPI-style export with pyDEXPI (optional)
    pip install "pydexpi[cli]"
    pydexpi info out_app_streams_aspen_10tpd/pid_dexpi_style.xml
The export was written without validation; pyDEXPI will report anything it cannot read.

## D. Things to know
* **Edits made in the app are temporary on the cloud.** They last until the app restarts. To keep them, edit the CSV
  files in `data/` and commit them to GitHub; the app redeploys by itself.
* **The AI key is billed per use** by Anthropic and is separate from a Claude subscription. Without a key the app
  still works; the "Ask the plant" tab then answers its built-in question types only.
* **Never commit the key.** `.streamlit/secrets.toml` is in `.gitignore`; use `.streamlit/secrets.toml.example` as the pattern.

## E. If something goes wrong
| Symptom | Fix |
|---|---|
| "streamlit is not recognized" | Activate the virtual environment again (step A2). |
| P&ID tab says Graphviz is not installed | Install Graphviz and add it to PATH; on the cloud check that `packages.txt` contains `graphviz`. |
| ModuleNotFoundError on the cloud | The package is missing from `requirements.txt`. |
| "Ask the plant" says the AI call failed | Check the key in Secrets and the model name (`ANTHROPIC_MODEL` environment variable). |
| A saved edit is rejected | The message names the unknown equipment tag, type or hint code; fix it in the table. |
