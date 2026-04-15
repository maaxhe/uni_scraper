# 📚 Stud.IP Dashboard

Hi dear Coxis and other students using Stud.IP. I built this tool to automatically download all files from your signed-in courses on Stud.IP without you logging in every time and downloading each file manually.
One button - and it's done! In this dashboard. Enjoy and use the extra time saved for a smile at the person in front of you :D (or even giving them a compliment).

Wish you the best,
Max

Automatically downloads all files from your Stud.IP courses, generates AI summaries, and displays everything in a clean local dashboard.

> **Note:** This tool is built for **Uni Osnabrück's Stud.IP** (`studip.uni-osnabrueck.de`). It may not work with other universities without code changes.

---

## What does it do?

- **Sync files** – Automatically download all lecture slides, PDFs, scripts, etc. from Stud.IP
- **AI summaries** – Let Claude (Anthropic) summarise your course materials for you
- **Dashboard** – Local web app with file browser, summaries, flashcards, notes and AI chat
- **Search** – Search across all your course contents at once

---

## What you need before starting

| Requirement                                | Why                                   |
| ------------------------------------------ | ------------------------------------- |
| Python 3.10 or newer                       | Runs the app                          |
| Your Stud.IP login credentials             | To download your course files         |
| A folder on your computer for course files | Where all downloaded files are stored |
| An Anthropic API key _(optional)_          | Only needed for AI summaries and chat |

---

## Step-by-Step Setup

### Step 1 – Install Python

Open a terminal and check if Python is already installed:

```
python3 --version
```

If you see a version number ≥ 3.10 (e.g. `Python 3.11.4`), skip to Step 2.

**Install Python:**

- **Mac:** Download from [python.org/downloads](https://www.python.org/downloads/) and run the installer
- **Windows:** Download from [python.org/downloads](https://www.python.org/downloads/), run the installer, and **make sure to check "Add Python to PATH"** before clicking Install

---

### Step 2 – Download this project

**Option A – with Git:**

```
git clone https://github.com/YOUR-USERNAME/uni_scraper.git
cd uni_scraper
```

**Option B – as ZIP (no Git needed):**

On GitHub: click **"Code"** (top right) → **"Download ZIP"** → unzip it → open the folder

---

### Step 3 – Open a terminal inside the project folder

- **Mac:** Right-click the `uni_scraper` folder → **"New Terminal at Folder"**
- **Windows:** Open the folder, then `Shift + Right-click` on an empty area → **"Open PowerShell window here"**

All following commands must be run from inside this folder.

---

### Step 4 – Install dependencies

Run these two commands one after the other:

```
pip install -r requirements.txt
```

```
python -m playwright install chromium
```

> ⏳ This may take 2–5 minutes. Wait until each command finishes before running the next.

> **Why Chromium?** The scraper logs into Stud.IP using a real browser session, which is required for the university's SSO login. Without it, the dashboard opens fine but the **"↓ Sync"** button will not work.

If `pip` is not found, try `pip3`. If `python` is not found, try `python3`.

---

### Step 5 – Create the folder for your course files

Create a folder somewhere on your computer where all downloaded course files will be stored. For example:

- **Mac:** `~/Documents/Uni/Courses`
- **Windows:** `C:\Users\yourname\Documents\Uni\Courses`

**The folder must exist before starting the app.** You can create it via Finder/Explorer or in the terminal:

```
mkdir -p ~/Documents/Uni/Courses
```

Note down the full path — you will need it in the next step.

---

### Step 6 – Create your configuration file

Copy the example file:

```
cp .env.example .env
```

On **Windows:**

```
copy .env.example .env
```

Open the new `.env` file in any text editor (Notepad, VS Code, TextEdit) and fill in your values:

```
STUDIP_USERNAME=your_uni_username
STUDIP_PASSWORD=your_password
ANTHROPIC_API_KEY=sk-ant-...
COURSES_DIR=/Users/yourname/Documents/Uni/Courses
```

**What goes where:**

| Field               | What to enter                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------ |
| `STUDIP_USERNAME`   | Your Uni Osnabrück username (usually your student ID or short name)                              |
| `STUDIP_PASSWORD`   | Your Uni Osnabrück password. If it contains special characters, wrap it in quotes: `"Pa$$word!"` |
| `ANTHROPIC_API_KEY` | Your API key (see Step 7). You can leave this blank for now if you only want to sync files.      |
| `COURSES_DIR`       | The **full path** to the folder you created in Step 5                                            |

**Example for Mac:**

```
STUDIP_USERNAME=mmueller
STUDIP_PASSWORD=MyPassword123
ANTHROPIC_API_KEY=sk-ant-abc123...
COURSES_DIR=/Users/mmueller/Documents/Uni/Courses
```

**Example for Windows:**

```
STUDIP_USERNAME=mmueller
STUDIP_PASSWORD=MyPassword123
ANTHROPIC_API_KEY=sk-ant-abc123...
COURSES_DIR=C:/Users/mmueller/Documents/Uni/Courses
```

> 🔒 This file stays on your computer only. It is listed in `.gitignore` and will never be uploaded to GitHub.

---

### Step 7 – Get an Anthropic API key _(optional — only for AI summaries)_

Skip this step if you only want to sync and browse files.

1. Go to [console.anthropic.com](https://console.anthropic.com) and create a free account
2. New accounts receive free trial credit — no credit card required
3. In the left menu click **"API Keys"** → **"Create Key"**
4. Copy the key and paste it into your `.env` file as `ANTHROPIC_API_KEY=sk-ant-...`

---

### Step 8 – Start the dashboard

```
python dashboard.py
```

On Mac/Linux you may need:

```
python3 dashboard.py
```

Then open your browser and go to:

**→ [http://localhost:5001](http://localhost:5001)**

The dashboard is now running. Keep the terminal open — closing it stops the app.

---

## First steps in the dashboard

1. Click **"↓ Sync"** in the top right corner → downloads all your files from Stud.IP  
   _(The first sync can take several minutes depending on how many files you have)_
2. Click a **course** in the left sidebar to open it
3. **"Files" tab** → browse and read files, write notes per file
4. **"Summary" tab** → click **"Summarize"** → AI creates a summary of the course  
   _(requires an API key from Step 7)_
5. **"Learn" tab** → flashcards automatically generated from the summary

---

## Troubleshooting

### `pip: command not found`

Use `pip3` instead:

```
pip3 install -r requirements.txt
python3 -m playwright install chromium
```

### `python: command not found`

Use `python3`:

```
python3 dashboard.py
```

### Dashboard doesn't open at localhost:5001

Another program may be using port 5001. Stop that program and restart the dashboard.

### Stud.IP login fails

- Double-check username and password in `.env`
- Special characters in the password must be wrapped in quotes: `STUDIP_PASSWORD="Pa$$word!"`
- Watch the browser during login to see what's happening: `python scraper.py --no-headless`

### "No such file or directory" when starting

Make sure `COURSES_DIR` in `.env` points to a folder that **actually exists** on your computer. Create the folder first if needed.

### Summary fails with an error

The dashboard shows a specific error message in the log area with instructions on how to fix it (e.g. missing API key, wrong key, no internet connection).

---

## Privacy

- **Stud.IP credentials** are sent only to Uni Osnabrück's servers — nowhere else
- **Course files** are stored locally on your computer and never uploaded
- **AI summaries** only transmit the text content of your files to Anthropic — not the files themselves
- The `.env` file with your credentials is never committed to Git
