# sgDesk

Aplikasi Remote Desktop (seperti AnyDesk) berbasis WebSocket & native Windows screen capture/input yang dirancang agar bekerja mulus melalui **Cloudflare Tunnel (`cloudflared`)** tanpa perlu port forwarding atau IP publik.

## Fitur Utama
- **Screen Streaming Cepat**: Menggunakan `mss` + JPEG adaptive encoding dengan skip frame identik (hemat bandwidth saat idle).
- **Kontrol Mouse & Keyboard Native**: Menggunakan Windows `SendInput` (`ctypes`), mendukung multi-monitor, DPI scaling, scroll, klik kanan/tengah, dan shortcut (`Win`, `Ctrl+Shift+Esc`, `Alt+Tab`, `Win+D`, `Win+L`).
- **Keamanan Berlapis**:
  - **Desk ID** & **One-Time 6-Digit PIN** (atau **Unattended Password** permanen).
  - **Mode Approval (Accept/Reject)** opsional seperti AnyDesk.
  - **Proteksi Admin Otomatis**: Panel Host (PIN, pengaturan, daftar sesi) hanya bisa diakses dari `localhost` tanpa header proxy Cloudflare. Pengunjung dari Cloudflare Tunnel hanya melihat halaman **Connect / Client**.
- **Integrasi Cloudflare Tunnel**:
  - Mendeteksi `cloudflared` dan worker yang sudah terpasang di komputer.
  - Mendukung **Named Tunnel** (domain sendiri) maupun **Quick Tunnel** (`*.trycloudflare.com`) sekali klik.
- **Clipboard Sync**: Kirim dan ambil teks clipboard antara komputer lokal dan remote.

## Cara Menjalankan
1. Jalankan **`sgdesk.bat`** (klik dua kali), atau lewat terminal:
   ```powershell
   d:\project\sgDesk\.venv\Scripts\python.exe sgdesk.py
   ```
2. Aplikasi akan berjalan di `http://localhost:3737` dan membuka jendela sgDesk secara otomatis.

## Menghubungkan ke Cloudflare Tunnel yang Sudah Terpasang
Karena `cloudflared` sudah terpasang di komputer Anda:
1. Di **Cloudflare Zero Trust Dashboard** -> **Networks** -> **Tunnels** -> pilih tunnel Anda -> **Public Hostname** -> **Add a public hostname**:
   - **Subdomain / Domain**: misalnya `sgdesk.domainanda.com`
   - **Service Type**: `HTTP`
   - **URL**: `localhost:3737`
   *(Cloudflare Tunnel otomatis mendukung WebSocket `wss://` untuk HTTP origin.)*
2. Simpan URL tersebut (mis. `https://sgdesk.domainanda.com`) di kolom **Your Tunnel URL** pada panel Cloudflare Tunnel di sgDesk agar link siap di-copy.
3. Dari perangkat mana saja (laptop, HP, browser), buka `https://sgdesk.domainanda.com`, masukkan **Access PIN** atau **Unattended Password**, lalu klik **Connect Now**.

> **Tips:** Jika ingin link sementara tanpa setting domain, klik tombol **Start Quick Tunnel** di dashboard sgDesk untuk mendapatkan URL `https://*.trycloudflare.com`.
