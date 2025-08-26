# 🛠 Uptime Kuma Backup Restore Script

This script restores **Uptime Kuma monitors and notifications** from a backup JSON export.  
It automatically recreates **groups, monitors, and notifications** in the correct order while handling API quirks gracefully.

---

## ✨ Features
- ✅ Restore monitors & notifications directly from a backup JSON  
- ✅ Preserves **group hierarchy** (topological order: parents before children)  
- ✅ Maps old → new notification/group IDs automatically  
- ✅ Supports **dry-run** mode (safe preview without changes)  
- ✅ Automatically retries API calls on Socket.IO issues (`BadNamespaceError`, `Timeout`)  
- ✅ Cleans payloads to **only include supported fields**  
- ✅ Detailed, timestamped logs with a final summary  

---

## 📦 Requirements
- Python **3.9+**  
- Uptime Kuma instance (tested with >= 1.23)  
- Installed Python package:  
  ```bash
  pip install uptime-kuma-api
  ```

---

## 🔧 Setup

1. Clone or download the script (`restore_kuma_from_backup.py`).  

2. Export the required environment variables:  

   ```bash
   export KUMA_URL="https://kuma.example.com"
   export KUMA_USERNAME="your-username"
   export KUMA_PASSWORD="your-password"
   ```

   *(Optional)* Adjust timeout (default `60s`):  
   ```bash
   export KUMA_TIMEOUT=90
   ```

3. Place your backup file (from Kuma’s **Settings → Backup → Export JSON**) somewhere accessible.  

---

## 🚀 Usage

Basic restore:  
```bash
python3 restore_kuma_from_backup.py --backup Uptime_Kuma_Backup.json
```

Dry-run (no changes, just prints actions):  
```bash
python3 restore_kuma_from_backup.py --backup Uptime_Kuma_Backup.json --dry-run
```

Skip creating notifications (monitors only):  
```bash
python3 restore_kuma_from_backup.py --backup Uptime_Kuma_Backup.json --skip-notifications
```

Restore **only active monitors**:  
```bash
python3 restore_kuma_from_backup.py --backup Uptime_Kuma_Backup.json --only-active
```

---

## 📊 Example Output

```
[2025-08-26 14:05:33] [INFO] Connecting to https://kuma.example.com as admin
[2025-08-26 14:05:33] [INFO] Creating notifications…
[2025-08-26 14:05:33] [SKIP] Notification 'Telegram' already exists -> id 1
[2025-08-26 14:05:33] [INFO] Creating groups…
[2025-08-26 14:05:33] [OK ] Created group 'Production Servers' -> id 12
[2025-08-26 14:05:34] [INFO] Creating monitors…
[2025-08-26 14:05:34] [OK ] Created monitor 'API Health Check' -> id 42
[2025-08-26 14:05:34] [OK ] Paused monitor 'Staging API'
[2025-08-26 14:05:34] [DONE] Groups in backup: 5; Monitors in backup: 37
[2025-08-26 14:05:34] [DONE] Monitors created: 37 (paused: 3, skipped: 1)
```

---

## ⚠️ Notes & Troubleshooting
- The script retries failed API calls once if the connection drops.  
- If using **macOS with LibreSSL**, you may see warnings about `urllib3`. Consider a Python build linked against OpenSSL.  
- If your Kuma server is slow to respond, increase the timeout:  
  ```bash
  export KUMA_TIMEOUT=120
  ```
- Groups are created with only minimal fields (`name`, `parent`) since Kuma rejects extra keys for groups.  

---

## 📜 License
MIT — feel free to fork, improve, and share.