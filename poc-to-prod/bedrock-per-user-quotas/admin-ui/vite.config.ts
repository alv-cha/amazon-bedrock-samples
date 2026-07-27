import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Static SPA served from S3 + CloudFront. Runtime config (gateway URL, Cognito
// ids) is provided by public/config.js so the same build can target any
// deployment — see src/config.ts.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
});
