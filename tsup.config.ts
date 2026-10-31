import { defineConfig } from "tsup";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    cli: "src/cli.ts",
    feishu: "src/feishu.ts",
    "core/index": "src/core/index.ts",
    "telegram/index": "src/telegram/index.ts",
    "telegram/polling-host": "src/telegram/polling-host.ts",
  },
  format: ["esm"],
  // node:sqlite has no bare "sqlite" builtin alias.
  removeNodeProtocol: false,
  dts: false,
  clean: false,
  sourcemap: true,
});
