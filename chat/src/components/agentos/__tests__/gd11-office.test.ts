import { describe, expect, it } from "vitest";
import { decodeMime, isOfficeTool, officeRows, officeTitle } from "../office-approval";

const CARD = [
  "Send a saved draft",
  "Draft: r-1 (its recipients are the ones saved in the draft)",
  "To: lan@example.com, minh@example.com",
  "Cc: sep@example.com",
  "Subject: Báo giá tháng 11",
  "Text: Chào chị Lan, em gửi báo giá như đã hẹn.",
  "Attachments: 2 (bao-gia.pdf, hop-dong.docx)",
].join("\n");

describe("11.62 office approval card", () => {
  it("recognises the office tools only", () => {
    for (const n of ["gmail_send", "gcal_create", "outlook_send_draft", "ocal_respond"]) expect(isOfficeTool(n)).toBe(true);
    for (const n of ["crm_log_deal", "send_gmail", "gmailsend"]) expect(isOfficeTool(n)).toBe(false);
  });
  it("shows To / Cc / subject / preview / attachments from the server card text", () => {
    const rows = officeRows({}, CARD, "en");
    const by = Object.fromEntries(rows.map((r) => [r.label, r.value]));
    expect(by["To"]).toBe("lan@example.com, minh@example.com");
    expect(by["Cc"]).toBe("sep@example.com");
    expect(by["Subject"]).toBe("Báo giá tháng 11");
    expect(by["Preview"]).toContain("báo giá");
    expect(by["Attachments"]).toContain("bao-gia.pdf");
  });
  it("labels in Vietnamese", () => {
    const labels = officeRows({}, CARD, "vi").map((r) => r.label);
    expect(labels).toEqual(expect.arrayContaining(["Đến", "Tiêu đề", "Tệp đính kèm"]));
  });
  it("shows attendees of an event and keeps the look-up warning visible", () => {
    const rows = officeRows({}, ["Create a calendar event", "Event: Họp", "Start: 2026-11-03T09:00", "These people will receive an invitation email: a@x.vn, b@x.vn"].join("\n"), "en");
    expect(rows.find((r) => r.label.startsWith("Attendees"))?.value).toBe("a@x.vn, b@x.vn");
    const warn = officeRows({}, "Send\nCẢNH BÁO: không xem được người nhận — từ chối nếu không chắc.", "vi");
    expect(warn[0].label).toBe("Cảnh báo");
    expect(warn[0].value).toContain("không xem được người nhận");
  });
  it("falls back to the arguments when there is no card text", () => {
    const rows = officeRows({ to: ["a@x.vn"], bcc: ["z@x.vn"], subject: "Hi", body: "Line one\nline two", attachments: ["f.pdf"], attendees: ["q@x.vn"] }, undefined, "en");
    const by = Object.fromEntries(rows.map((r) => [r.label, r.value]));
    expect(by["To"]).toBe("a@x.vn");
    expect(by["Bcc"]).toBe("z@x.vn");
    expect(by["Preview"]).toBe("Line one line two");
    expect(by["Attachments"]).toBe("1 (f.pdf)");
  });
  it("titles in both languages", () => {
    expect(officeTitle("gmail_send_draft", "vi")).toBe("Gửi bản nháp");
    expect(officeTitle("ocal_respond", "en")).toBe("Answer an invitation");
    expect(officeTitle("gmail_unknown", "en")).toBe("gmail_unknown");
  });
  it("decodes RFC 2047 names and leaves broken words alone", () => {
    expect(decodeMime("=?utf-8?b?Q2jhu4sgTGFu?= <lan@khach.vn>")).toBe("Chị Lan <lan@khach.vn>");
    expect(decodeMime("=?UTF-8?Q?Ch=E1=BB=8B_Lan?= <a@b.vn>")).toBe("Chị Lan <a@b.vn>");
    expect(decodeMime("=?utf-8?b?Q2g=?= =?utf-8?b?4buLIExhbg==?=")).toBe("Chị Lan");
    expect(decodeMime("=?nope?b?***?= x")).toBe("=?nope?b?***?= x");
    expect(decodeMime("plain <a@b.vn>")).toBe("plain <a@b.vn>");
  });
  it("puts who gets the mail first and the text last, with the looked-up recipient", () => {
    const card = ["Reply to an email", "To: the sender of message m-1", "Text: Chào chị", "Will be sent to: =?utf-8?b?Q2jhu4sgTGFu?= <lan@khach.vn>", "Subject: Re: Báo giá"].join("\n");
    const rows = officeRows({}, card, "en");
    expect(rows.map((r) => r.label)).toEqual(["Will be sent to (looked up)", "Subject", "Preview"]);
    expect(rows[0].value).toBe("Chị Lan <lan@khach.vn>");
  });
});
