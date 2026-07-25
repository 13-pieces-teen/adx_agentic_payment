import { readFile, stat } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { getAddress, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";

export class FacilitatorCsvError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "FacilitatorCsvError";
  }
}

export async function loadFacilitatorPrivateKey(
  path: string,
  facilitatorIndex: string,
): Promise<Hex> {
  if (!isAbsolute(path)) {
    throw new FacilitatorCsvError("facilitator_csv_path_must_be_absolute");
  }
  const metadata = await stat(path);
  if (!metadata.isFile()) {
    throw new FacilitatorCsvError("facilitator_csv_not_regular");
  }
  if ((metadata.mode & 0o077) !== 0) {
    throw new FacilitatorCsvError("facilitator_csv_permissions");
  }
  if (!/^[1-9][0-9]*$/.test(facilitatorIndex)) {
    throw new FacilitatorCsvError("facilitator_index_invalid");
  }
  const rows = parseCsv(await readFile(path, "utf8"));
  const header = rows.shift();
  if (!header) throw new FacilitatorCsvError("facilitator_csv_invalid");
  const indexColumn = header.indexOf("facilitator_index");
  const addressColumn = header.indexOf("ethereum_address");
  const keyColumn = header.indexOf("private_key");
  if (Math.min(indexColumn, addressColumn, keyColumn) < 0) {
    throw new FacilitatorCsvError("facilitator_csv_invalid");
  }
  let match: Hex | undefined;
  for (const row of rows) {
    if (row.length !== header.length) {
      throw new FacilitatorCsvError("facilitator_csv_invalid");
    }
    const index = row[indexColumn]?.trim();
    const address = row[addressColumn]?.trim();
    const key = row[keyColumn]?.trim();
    if (
      !index ||
      !address ||
      !key ||
      !/^0x[0-9a-fA-F]{64}$/.test(key)
    ) {
      throw new FacilitatorCsvError("facilitator_csv_invalid");
    }
    let derived: string;
    try {
      derived = privateKeyToAccount(key as Hex).address;
      if (getAddress(derived) !== getAddress(address)) {
        throw new FacilitatorCsvError("facilitator_csv_invalid");
      }
    } catch (error) {
      if (error instanceof FacilitatorCsvError) throw error;
      throw new FacilitatorCsvError("facilitator_csv_invalid");
    }
    if (index === facilitatorIndex) {
      if (match) {
        throw new FacilitatorCsvError("facilitator_index_duplicate");
      }
      match = key as Hex;
    }
  }
  if (!match) throw new FacilitatorCsvError("facilitator_index_not_found");
  return match;
}

function parseCsv(content: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < content.length; index += 1) {
    const character = content[index];
    if (quoted) {
      if (character === '"' && content[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        field += character;
      }
    } else if (character === '"') {
      if (field) throw new FacilitatorCsvError("facilitator_csv_invalid");
      quoted = true;
    } else if (character === ",") {
      row.push(field);
      field = "";
    } else if (character === "\n") {
      row.push(field.replace(/\r$/, ""));
      if (row.some((value) => value !== "")) rows.push(row);
      row = [];
      field = "";
    } else {
      field += character;
    }
  }
  if (quoted) throw new FacilitatorCsvError("facilitator_csv_invalid");
  row.push(field.replace(/\r$/, ""));
  if (row.some((value) => value !== "")) rows.push(row);
  return rows;
}
