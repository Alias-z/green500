// Give each generated browser asset a content-specific URL after compilation.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const staticDirectory = new URL("../green500/static/", import.meta.url);
for (const name of ["index.html", "portfolio.html"]) {
  const path = new URL(name, staticDirectory);
  const original = readFileSync(path, "utf8");
  const updated = original.replace(
    /((?:src|href)="\/static\/)([A-Za-z0-9_.-]+\.(?:js|css))(?:\?[^"\s]*)?("?)/g,
    (_match, prefix, asset, suffix) => {
      const digest = createHash("sha256")
        .update(readFileSync(new URL(asset, staticDirectory)))
        .digest("hex").slice(0, 16);
      return `${prefix}${asset}?v=${digest}${suffix}`;
    },
  );
  if (updated !== original) writeFileSync(path, updated);
}
