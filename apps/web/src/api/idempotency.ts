type KeyFactory = () => string;

type Attempt = {
	fingerprint: string;
	key: string;
};

export class LogicalOperationKeys {
	readonly #createKey: KeyFactory;
	#attempt: Attempt | null = null;

	constructor(createKey: KeyFactory = () => crypto.randomUUID()) {
		this.#createKey = createKey;
	}

	forPayload(payload: unknown): string {
		let fingerprint: string | undefined;
		try {
			fingerprint = JSON.stringify(payload);
		} catch {
			throw new TypeError(
				"The logical operation payload is not JSON encodable.",
			);
		}
		if (fingerprint === undefined) {
			throw new TypeError(
				"The logical operation payload is not JSON encodable.",
			);
		}
		if (this.#attempt?.fingerprint === fingerprint) {
			return this.#attempt.key;
		}
		const key = this.#createKey();
		if (key.length < 8) {
			throw new TypeError(
				"The idempotency key factory returned an invalid key.",
			);
		}
		this.#attempt = { fingerprint, key };
		return key;
	}

	complete(key: string): void {
		if (this.#attempt?.key === key) {
			this.#attempt = null;
		}
	}

	reset(): void {
		this.#attempt = null;
	}
}
