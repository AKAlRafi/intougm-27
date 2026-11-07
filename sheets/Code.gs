function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents || "{}");
    const properties = PropertiesService.getScriptProperties();
    if (!data.token || data.token !== properties.getProperty("INTO_SHEETS_TOKEN")) {
      return json({ ok: false, message: "Unauthorized" });
    }

    if (data.type === "paid_order") {
      return savePaidOrder(data, properties);
    }

    const expected = { "Single": 1, "Child-Free/Duo": 2, "Keluarga Berencana/Trio": 3, "Keluarga Besar/Circle": 5 }[data.paket];
    if (!expected || !Array.isArray(data.peserta) || data.peserta.length !== expected) {
      throw new Error("Jumlah peserta tidak sesuai paket.");
    }

    const spreadsheet = SpreadsheetApp.openById(properties.getProperty("INTO_SPREADSHEET_ID"));
    const sheet = spreadsheet.getSheetByName("Pemesanan") || spreadsheet.insertSheet("Pemesanan");
    prepareRegistrationSheet(sheet);

    if (data.bukti && data.bukti.base64) {
      const names = data.peserta.map((person) => safeFilenamePart(person.nama));
      const extension = fileExtension(data.bukti.filename);
      const filename = `(${names.join("),(")}).${extension}`;
      const bytes = Utilities.base64Decode(data.bukti.base64);
      const blob = Utilities.newBlob(bytes, data.bukti.contentType, filename);
      const folderId = properties.getProperty("INTO_PROOF_FOLDER_ID");
      if (folderId) {
        DriveApp.getFolderById(folderId).createFile(blob);
      } else {
        DriveApp.createFile(blob);
      }
    }

    const rows = data.peserta.map((person) => [
      safeCell(person.nama),
      safeCell(person.sekolah),
      safeCell(person.whatsapp),
    ]);
    const firstRow = sheet.getLastRow() + 1;
    sheet.getRange(firstRow, 3, rows.length, 1).setNumberFormat("@");
    sheet.getRange(firstRow, 1, rows.length, 3).setValues(rows);
    return json({ ok: true });
  } catch (error) {
    return json({ ok: false, message: String(error) });
  }
}

function savePaidOrder(data, properties) {
  if (data.status !== "PAID") {
    throw new Error("Hanya transaksi PAID yang dapat dicatat.");
  }
  const order = data.order || {};
  const expected = { "Single": 1, "Child-Free/Duo": 2, "Keluarga Berencana/Trio": 3, "Keluarga Besar/Circle": 5 }[order.package];
  if (
    !order.id ||
    !expected ||
    !Number.isInteger(Number(order.amount)) ||
    Number(order.amount) < 0 ||
    !Array.isArray(data.participants) ||
    data.participants.length !== expected ||
    data.participants.some((person) => !person.full_name || !person.ticket_code)
  ) {
    throw new Error("Data transaksi PAID tidak lengkap atau tidak sesuai paket.");
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const spreadsheet = SpreadsheetApp.openById(properties.getProperty("INTO_SPREADSHEET_ID"));
    const sheet = spreadsheet.getSheetByName("Pembayaran") || spreadsheet.insertSheet("Pembayaran");
    const headers = [
      "Order ID",
      "Status",
      "Waktu Bayar",
      "Paket",
      "Total Dibayar (Rp)",
      "Diskon (Rp)",
      "Voucher",
      "Nama Pendaftar",
      "Email Pendaftar",
      "WhatsApp Pendaftar",
      "Nama Peserta",
      "Asal Sekolah",
      "Email Peserta",
      "WhatsApp Peserta",
      "Kode Tiket",
    ];

    if (sheet.getLastRow() === 0) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    } else {
      const currentHeaders = sheet.getRange(1, 1, 1, headers.length).getDisplayValues()[0];
      if (headers.some((header, index) => currentHeaders[index] !== header)) {
        throw new Error("Header tab Pembayaran berbeda dari format yang diperlukan.");
      }
    }

    const lastRow = sheet.getLastRow();
    if (lastRow > 1) {
      const knownOrders = sheet
        .getRange(2, 1, lastRow - 1, 1)
        .getDisplayValues()
        .flat();
      if (knownOrders.includes(String(order.id))) {
        return json({ ok: true, duplicate: true, order_id: String(order.id) });
      }
    }

    const rows = data.participants.map((person) => [
      safeCell(order.id),
      "PAID",
      safeCell(order.paid_at),
      safeCell(order.package),
      Number(order.amount),
      Number(order.discount_amount || 0),
      safeCell(order.voucher_code),
      safeCell(order.buyer_name),
      safeCell(order.buyer_email),
      safeCell(order.buyer_phone),
      safeCell(person.full_name),
      safeCell(person.school),
      safeCell(person.email),
      safeCell(person.phone),
      safeCell(person.ticket_code),
    ]);
    const firstRow = lastRow + 1;
    sheet.getRange(firstRow, 1, rows.length, headers.length).setValues(rows);
    sheet.getRange(firstRow, 1, rows.length, 1).setNumberFormat("@");
    sheet.getRange(firstRow, 10, rows.length, 1).setNumberFormat("@");
    sheet.getRange(firstRow, 14, rows.length, 2).setNumberFormat("@");
    return json({ ok: true, order_id: String(order.id), rows: rows.length });
  } finally {
    lock.releaseLock();
  }
}

function prepareRegistrationSheet(sheet) {
  const headers = ["Nama", "Sekolah", "No. Telp"];
  ensureBlankColumns(sheet);
  const values = sheet.getDataRange().getValues();

  if (values.length === 0 || values[0].every((value) => value === "")) {
    sheet.clearContents();
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    return;
  }

  const headerRow = findHeaderRow(values);
  if (!headerRow) {
    throw new Error(
      "Judul kolom tidak dikenali. Gunakan Nama, Sekolah, dan No. Telp/WA pada satu baris header.",
    );
  }

  if (
    headerRow.name === 0 &&
    headerRow.school === 1 &&
    headerRow.phone === 2 &&
    headerRow.row === 0 &&
    headers.every((header, index) => String(values[0][index]).trim() === header)
  ) {
    normalizeExistingPhones(sheet);
    return;
  }

  const previousEntries = values
    .slice(headerRow.row + 1)
    .map((row) => [row[headerRow.name], row[headerRow.school], phoneAsText(row[headerRow.phone])])
    .filter((row) => row.some((value) => value !== ""));

  sheet.clearContents();
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  if (previousEntries.length) {
    sheet.getRange(2, 3, previousEntries.length, 1).setNumberFormat("@");
    sheet.getRange(2, 1, previousEntries.length, headers.length).setValues(previousEntries);
  }
}

function findHeaderRow(values) {
  const rowsToCheck = Math.min(values.length, 10);
  for (let rowIndex = 0; rowIndex < rowsToCheck; rowIndex++) {
    const cells = values[rowIndex].map((value) =>
      String(value)
        .toLowerCase()
        .replace(/[^a-z0-9]/g, ""),
    );
    const name = cells.findIndex((value) => value.includes("nama"));
    const school = cells.findIndex((value) => value.includes("sekolah"));
    const phone = cells.findIndex((value) =>
      [
        "whatsapp",
        "telp",
        "telepon",
        "phone",
        "ponsel",
        "handphone",
        "nomorwa",
        "nowa",
        "nohp",
        "nomorhp",
      ].some((key) => value.includes(key)),
    );
    if (name >= 0 && school >= 0 && phone >= 0) {
      return { row: rowIndex, name, school, phone };
    }
  }
  return null;
}

function normalizeExistingPhones(sheet) {
  const count = sheet.getLastRow() - 1;
  if (count <= 0) {
    return;
  }
  const range = sheet.getRange(2, 3, count, 1);
  range.setNumberFormat("@");
  range.setValues(range.getValues().map(([phone]) => [phoneAsText(phone)]));
}

function phoneAsText(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    const digits = String(Math.trunc(value));
    if (/^[1-9]\d{9,11}$/.test(digits) && !digits.startsWith("62")) {
      return `0${digits}`;
    }
    return digits;
  }
  return String(value || "").trim();
}

function ensureBlankColumns(sheet) {
  const standardColumnCount = 26;
  const missingColumns = standardColumnCount - sheet.getMaxColumns();
  if (missingColumns > 0) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), missingColumns);
  }
}

function safeFilenamePart(value) {
  return String(value || "Peserta")
    .replace(/[\\/:*?"<>|()[\]]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 35);
}

function fileExtension(value) {
  const match = String(value || "")
    .toLowerCase()
    .match(/\.(jpg|jpeg|png|webp)$/);
  return match ? match[1] : "jpg";
}

function safeCell(value) {
  const text = String(value || "");
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

function json(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(
    ContentService.MimeType.JSON,
  );
}
