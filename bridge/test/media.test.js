import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { safeMediaName, shouldDownload } from "../src/wa.js";

const RULES = { extensions: [".xlsx", ".xlsm"], maxBytes: 10 * 1024 * 1024 };

function doc(overrides = {}) {
  return {
    id: "AAA111",
    type: "document",
    filename: "Bost_1104999.xlsx",
    media_size: 7398,
    ...overrides,
  };
}

describe("safeMediaName", () => {
  it("prefixes with the message id", () => {
    assert.equal(safeMediaName("AAA111", "Bost_1104999.xlsx"), "AAA111-Bost_1104999.xlsx");
  });

  it("strips directory components", () => {
    // Everything here is attacker-controlled: a sender picks their own filename.
    assert.equal(safeMediaName("A", "../../etc/passwd"), "A-passwd");
    assert.equal(safeMediaName("A", "/etc/shadow"), "A-shadow");
  });

  it("strips leading dots and exotic characters", () => {
    assert.equal(safeMediaName("A", ".bashrc"), "A-bashrc");
    assert.equal(safeMediaName("A", "fichier 军绿.xlsx"), "A-fichier___.xlsx");
  });

  it("sanitises the message id too", () => {
    assert.equal(safeMediaName("../x", "a.xlsx"), "___x-a.xlsx");
  });

  it("falls back when the name is unusable", () => {
    assert.equal(safeMediaName("A", ""), "A-piece-jointe");
    assert.equal(safeMediaName("A", "..."), "A-piece-jointe");
  });

  it("caps the length", () => {
    const name = safeMediaName("A", `${"x".repeat(400)}.xlsx`);
    assert.ok(name.length <= 130, name.length);
  });
});

describe("shouldDownload", () => {
  it("accepts an allowed spreadsheet", () => {
    assert.equal(shouldDownload(doc(), RULES), true);
  });

  it("is case insensitive on the extension", () => {
    assert.equal(shouldDownload(doc({ filename: "CMD.XLSX" }), RULES), true);
  });

  it("refuses another extension", () => {
    assert.equal(shouldDownload(doc({ filename: "photo.pdf" }), RULES), false);
  });

  it("refuses anything that is not a document", () => {
    assert.equal(shouldDownload(doc({ type: "image" }), RULES), false);
    assert.equal(shouldDownload(doc({ type: "text", filename: null }), RULES), false);
  });

  it("refuses a file declared over the cap", () => {
    assert.equal(shouldDownload(doc({ media_size: 20 * 1024 * 1024 }), RULES), false);
  });

  it("accepts every document when no extension is configured", () => {
    assert.equal(shouldDownload(doc({ filename: "x.pdf" }), { ...RULES, extensions: [] }), true);
  });
});
