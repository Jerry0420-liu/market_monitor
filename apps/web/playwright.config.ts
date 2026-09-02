import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
	fullyParallel: false,
	outputDir: "test-results",
	reporter: [["list"]],
	testDir: "./e2e",
	use: {
		baseURL: "http://127.0.0.1:4173",
		screenshot: "only-on-failure",
		trace: "retain-on-failure",
	},
	projects: [
		{
			name: "chromium",
			use: { ...devices["Desktop Chrome"] },
		},
	],
	webServer: {
		command: "npm run dev -- --host 127.0.0.1 --port 4173",
		reuseExistingServer: false,
		timeout: 120_000,
		url: "http://127.0.0.1:4173",
	},
	workers: 1,
});
