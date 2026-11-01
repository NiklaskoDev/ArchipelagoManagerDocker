// Makes the web UI's files smaller for the image, in place (Dockerfile, stage "assets"):
//
// - JavaScript and CSS without comments and spaces, and with short local names (esbuild, one file at a time: the
//   browser still loads them as the separate files they are). The sources in manager/ keep their comments.
// - The translations (JSON) without spaces.
// - Next to each text file a gzip copy (app.js.gz), which the server sends to browsers that take it (main.py).
//
//   npm install esbuild && node build-assets.mjs path/to/static
//
// esbuild is looked up from the current directory.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";

const esbuild = createRequire(path.join(process.cwd(), "/"))("esbuild");
const root = path.resolve(process.argv[2] || "");
if (!process.argv[2] || !fs.statSync(root).isDirectory()) {
  console.error("usage: node build-assets.mjs <static folder>");
  process.exit(2);
}

const files = fs.readdirSync(root, { recursive: true }).map((name) => path.join(root, name))
  .filter((file) => fs.statSync(file).isFile() && !file.endsWith(".gz"));

// Plain scripts (<script src> without type="module", such as theme.js) are not modules: their top-level names are
// global and must stay as they are. Every other .js file is a module.
const plainScripts = new Set();
for (const page of files.filter((file) => file.endsWith(".html"))) {
  for (const [tag, src] of fs.readFileSync(page, "utf8").matchAll(/<script\b[^>]*\bsrc="([^"]+)"[^>]*>/g)) {
    if (!/\btype="module"/.test(tag)) plainScripts.add(path.resolve(path.dirname(page), src));
  }
}

let before = 0, after = 0, compressed = 0;
for (const file of files) {
  const source = fs.readFileSync(file, "utf8");
  let output = null;
  if (file.endsWith(".js") || file.endsWith(".css")) {
    const css = file.endsWith(".css");
    const result = await esbuild.transform(source, {
      loader: css ? "css" : "js", minify: true, charset: "utf8", legalComments: "none", sourcefile: path.relative(root, file),
      ...(css || plainScripts.has(file) ? {} : { format: "esm" }),
    });
    for (const warning of result.warnings) console.warn(`${path.relative(root, file)}: ${warning.text}`);
    output = result.code;
  } else if (file.endsWith(".json")) {
    output = JSON.stringify(JSON.parse(source));
  }
  if (output !== null) {
    before += Buffer.byteLength(source);
    after += Buffer.byteLength(output);
    fs.writeFileSync(file, output);
  }
  if (/\.(js|css|json|html|svg)$/.test(file)) {
    const data = fs.readFileSync(file);
    const gz = zlib.gzipSync(data, { level: 9 });
    if (gz.length < data.length) {
      fs.writeFileSync(`${file}.gz`, gz);
      // the same time as the file itself: Last-Modified says when the file changed, not when it was compressed
      const { atime, mtime } = fs.statSync(file);
      fs.utimesSync(`${file}.gz`, atime, mtime);
      compressed += gz.length;
    }
  }
}
const kib = (n) => `${(n / 1024).toFixed(1)} KiB`;
console.log(`web UI: ${kib(before)} of scripts, styles and translations made ${kib(after)}; compressed copies ${kib(compressed)}`);
