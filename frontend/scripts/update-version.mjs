import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const cwd = fileURLToPath(new URL("..", import.meta.url));
const formatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "2-digit", month: "numeric", day: "numeric"
});
function dateVersion(date) {
  const parts = formatter.formatToParts(date);
  return ["year", "month", "day"].map(type => {
    const value = parts.find(part => part.type === type).value;
    return type === "year" ? value : String(Number(value));
  }).join(".");
}

const branch = execFileSync("git", ["branch", "--show-current"], { cwd, encoding: "utf8" }).trim();
if (branch !== "main") throw new Error("请在 main 上为下一次提交生成版本号。");
const today = dateVersion(new Date());
const timestamps = execFileSync("git", ["log", "--first-parent", "--format=%ct"], { cwd, encoding: "utf8" }).trim().split("\n");
const count = timestamps.filter(time => dateVersion(new Date(Number(time) * 1000)) === today).length;
const version = `${today}.${count + 1}`;
writeFileSync(new URL("../src/version.json", import.meta.url), `${JSON.stringify({ version }, null, 2)}\n`);
console.log(version);
