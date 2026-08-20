import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Static SPA served from S3 + CloudFront. Runtime config (gateway URL, Cognito
// ids) is provided by public/config.js so the same build can target any
// deployment — see src/config.ts.
export default defineConfig({
  plugins: [react()],
  // amazon-cognito-identity-js still references the Node global alias in its
  // browser bundle. Map it to the standard browser global at build time.
  define: { global: "globalThis" },
  build: { outDir: "dist", sourcemap: false },
});
