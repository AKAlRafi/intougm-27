# Sambungkan formulir ke Google Sheets

Integrasi ini membutuhkan satu kali konfigurasi oleh pemilik spreadsheet dan Google Drive. Situs
tidak menganggap pemesanan berhasil sampai Apps Script mengonfirmasi baris berhasil dicatat.

1. Buat Google Spreadsheet baru untuk pendaftaran, lalu salin ID dari URL di antara `/d/` dan
   `/edit`.
2. Buka **Extensions → Apps Script** dari spreadsheet itu. Salin isi `sheets/Code.gs` ke editor Apps
   Script dan simpan.
3. Di **Project Settings → Script Properties**, buat properti:
   - `INTO_SPREADSHEET_ID`: ID spreadsheet dari langkah pertama.
   - `INTO_SHEETS_TOKEN`: buat nilai rahasia acak yang panjang.
   - `INTO_PROOF_FOLDER_ID`: (opsional) ID folder Drive khusus untuk bukti pembayaran.
4. Pilih **Deploy → New deployment → Web app** dan jalankan sebagai akun pemilik spreadsheet. Supaya
   server Flask bisa mengirim data tanpa login Google, pilih akses yang dapat menerima permintaan
   eksternal (biasanya **Anyone**). Token rahasia tetap divalidasi oleh Apps Script dan hanya
   disimpan sebagai environment variable di server Flask, tidak pernah dikirim dari browser.
5. Salin URL Web App yang berakhiran `/exec` ke `INTO_SHEETS_WEBHOOK_URL`, lalu isi
   `INTO_SHEETS_TOKEN` di environment tempat Flask berjalan dengan nilai yang sama seperti Script
   Properties. Jangan masukkan token ke HTML atau unggah file `.env` ke Git.
6. Untuk checkout tiket, isi `MIDTRANS_SERVER_KEY` dan `MIDTRANS_CLIENT_KEY` dengan pasangan key
   dari environment Midtrans yang sama. Gunakan pasangan sandbox untuk uji coba dan
   `MIDTRANS_IS_PRODUCTION=false`.
7. Pasang dependensi dari `requirements.txt` dan jalankan ulang Flask. Perubahan `.env` baru dibaca
   saat server mulai.

Data pendaftar yang mengirim formulir lama melalui `/api/pemesanan` tetap masuk ke tab `Pemesanan`.
Pembelian paket baru masuk ke tab `Pembayaran` **hanya setelah Midtrans mengonfirmasi status PAID**.
Tab tersebut menyimpan satu baris per peserta dengan ID pesanan, waktu dan jumlah pembayaran, paket,
diskon/voucher, data pendaftar, data peserta, serta kode tiket. ID pesanan mencegah satu order
tercatat dua kali jika callback dicoba ulang. Baris pembayaran tidak dibuat oleh callback pending,
batal, ditolak, atau kedaluwarsa.

Jika integrasi sudah pernah dipasang, salin versi terbaru `sheets/Code.gs` ke Apps Script lalu pilih
**Deploy → Manage deployments → Edit → New version → Deploy**. URL `/exec` yang sama biasanya tetap
berlaku. Pastikan akun pemilik telah memberikan izin Spreadsheet setelah perubahan skrip.

Untuk callback Midtrans dapat menjangkau Flask, situs harus tersedia di URL publik HTTPS.
`127.0.0.1` di komputer lokal tidak dapat menerima callback dari server Midtrans. Gunakan deployment
HTTPS atau tunnel publik untuk pengujian lokal, lalu daftarkan URL publik
`/api/midtrans/notification` pada pengaturan Payment Notification URL Midtrans.
