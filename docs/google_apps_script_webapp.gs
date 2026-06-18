const SUMMARY_SHEET_NAME = "总分表";
const DETAIL_SHEET_NAME = "评分明细表";

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents || "{}");
    const expectedSecret = PropertiesService.getScriptProperties().getProperty("PAPER_GRADING_SECRET");
    if (!expectedSecret) {
      // fail-closed：未配置密钥则拒绝，避免端点无鉴权对外开放
      return jsonResponse({ ok: false, error: "server misconfigured: secret not set" });
    }
    if (body.secret !== expectedSecret) {
      return jsonResponse({ ok: false, error: "unauthorized" });
    }

    const spreadsheet = body.target_id ? SpreadsheetApp.openById(body.target_id) : SpreadsheetApp.getActiveSpreadsheet();
    if (!spreadsheet) {
      return jsonResponse({ ok: false, error: "spreadsheet not found" });
    }

    const payload = body.payload || {};
    const summaryRowsWritten = appendRows(
      spreadsheet,
      SUMMARY_SHEET_NAME,
      payload.summary_headers || [],
      payload.summary_row ? [payload.summary_row] : [],
    );
    const detailRowsWritten = appendRows(
      spreadsheet,
      DETAIL_SHEET_NAME,
      payload.detail_headers || [],
      payload.detail_rows || [],
    );

    return jsonResponse({
      ok: true,
      spreadsheet_id: spreadsheet.getId(),
      spreadsheet_url: spreadsheet.getUrl(),
      summary_rows_written: summaryRowsWritten,
      detail_rows_written: detailRowsWritten,
      run_id: body.run_id || "",
    });
  } catch (error) {
    return jsonResponse({ ok: false, error: String(error && error.message ? error.message : error) });
  }
}

function appendRows(spreadsheet, sheetName, headers, rows) {
  if (!headers.length || !rows.length) return 0;

  const sheet = spreadsheet.getSheetByName(sheetName) || spreadsheet.insertSheet(sheetName);
  ensureHeader(sheet, headers);

  const values = rows.map((row) => headers.map((header) => normalizeCell(row[header])));
  sheet.getRange(sheet.getLastRow() + 1, 1, values.length, headers.length).setValues(values);
  sheet.autoResizeColumns(1, headers.length);
  return values.length;
}

function ensureHeader(sheet, headers) {
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    styleHeader(sheet, headers.length);
    return;
  }

  const current = sheet.getRange(1, 1, 1, Math.max(headers.length, sheet.getLastColumn())).getValues()[0];
  const alreadyMatches = headers.every((header, index) => current[index] === header);
  if (!alreadyMatches) {
    // 就地更新第 1 行表头；勿用 insertRowBefore(1)，否则旧表头被挤成孤儿数据行
    const maxCols = Math.max(headers.length, sheet.getLastColumn());
    sheet.getRange(1, 1, 1, maxCols).clearContent();
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    styleHeader(sheet, headers.length);
  }
}

function styleHeader(sheet, width) {
  const range = sheet.getRange(1, 1, 1, width);
  range.setFontWeight("bold");
  range.setBackground("#DCEBFF");
  sheet.setFrozenRows(1);
}

function normalizeCell(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join("；");
  if (typeof value === "object") return JSON.stringify(value);
  return value;
}

function jsonResponse(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON);
}
