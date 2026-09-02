import { describe, expect, it, vi } from "vitest";
import { LogicalOperationKeys } from "./idempotency";

describe("logical-operation idempotency keys", () => {
	it("reuses the key for an unchanged lost-response retry", () => {
		const createKey = vi
			.fn()
			.mockReturnValueOnce("operation-one")
			.mockReturnValueOnce("operation-two");
		const keys = new LogicalOperationKeys(createKey);

		const first = keys.forPayload({ enabled: true });
		const retry = keys.forPayload({ enabled: true });

		expect(first).toBe("operation-one");
		expect(retry).toBe(first);
		expect(createKey).toHaveBeenCalledTimes(1);
	});

	it("rotates for a changed payload and after the prior operation completes", () => {
		const createKey = vi
			.fn()
			.mockReturnValueOnce("operation-one")
			.mockReturnValueOnce("operation-two")
			.mockReturnValueOnce("operation-three");
		const keys = new LogicalOperationKeys(createKey);

		const first = keys.forPayload({ enabled: true });
		const changed = keys.forPayload({ enabled: false });
		keys.complete(changed);
		const nextUserOperation = keys.forPayload({ enabled: false });

		expect(first).toBe("operation-one");
		expect(changed).toBe("operation-two");
		expect(nextUserOperation).toBe("operation-three");
	});

	it("does not clear a newer attempt when an older response finishes late", () => {
		const createKey = vi
			.fn()
			.mockReturnValueOnce("operation-one")
			.mockReturnValueOnce("operation-two");
		const keys = new LogicalOperationKeys(createKey);
		const older = keys.forPayload({ subject_uid: "one" });
		const newer = keys.forPayload({ subject_uid: "two" });

		keys.complete(older);

		expect(keys.forPayload({ subject_uid: "two" })).toBe(newer);
	});
});
