import {
	createContext,
	useCallback,
	useContext,
	useEffect,
	useMemo,
	useState,
} from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import type { SessionView } from "../api/generated";
import { api } from "../api/resources";
import type { MarketMonitorApi } from "../api/resources";

const CSRF_STORAGE_KEY = "market-monitor.csrf";

type AuthApi = Pick<MarketMonitorApi, "login" | "logout" | "session">;
type AuthStatus = "checking" | "authenticated" | "anonymous" | "error";

type AuthValue = {
	canWrite: boolean;
	csrfToken: string | null;
	error: ApiError | null;
	login: (username: string, password: string) => Promise<void>;
	logout: () => Promise<void>;
	markAnonymous: () => void;
	session: SessionView | null;
	status: AuthStatus;
};

const AuthContext = createContext<AuthValue | null>(null);

type AuthProviderProps = {
	children: ReactNode;
	client?: AuthApi;
};

export function AuthProvider({ children, client = api }: AuthProviderProps) {
	const [status, setStatus] = useState<AuthStatus>("checking");
	const [session, setSession] = useState<SessionView | null>(null);
	const [csrfToken, setCsrfToken] = useState<string | null>(readCsrf);
	const [error, setError] = useState<ApiError | null>(null);

	const clearLocal = useCallback(() => {
		removeCsrf();
		setCsrfToken(null);
		setSession(null);
		setError(null);
		setStatus("anonymous");
	}, []);

	useEffect(() => {
		const controller = new AbortController();
		void client
			.session(controller.signal)
			.then((response) => {
				if (controller.signal.aborted) {
					return;
				}
				setSession(response.data);
				setError(null);
				setStatus("authenticated");
			})
			.catch((caught: unknown) => {
				if (controller.signal.aborted) {
					return;
				}
				if (caught instanceof ApiError && caught.status === 401) {
					clearLocal();
					return;
				}
				setError(safeAuthError(caught));
				setStatus("error");
			});
		return () => controller.abort();
	}, [clearLocal, client]);

	const login = useCallback(
		async (username: string, password: string) => {
			setError(null);
			setStatus("checking");
			try {
				const response = await client.login(username, password);
				if (!response.csrfToken) {
					throw new ApiError(
						"The authenticated response did not include CSRF protection.",
						0,
						"CSRF_TOKEN_MISSING",
					);
				}
				writeCsrf(response.csrfToken);
				setCsrfToken(response.csrfToken);
				setSession(response.data);
				setStatus("authenticated");
			} catch (caught) {
				const safeError = safeAuthError(caught);
				removeCsrf();
				setCsrfToken(null);
				setSession(null);
				setError(safeError);
				setStatus("anonymous");
				throw safeError;
			}
		},
		[client],
	);

	const logout = useCallback(async () => {
		if (!csrfToken) {
			const missing = new ApiError(
				"Sign in again before ending this session.",
				401,
				"CSRF_TOKEN_MISSING",
			);
			setError(missing);
			throw missing;
		}
		setError(null);
		try {
			await client.logout(csrfToken);
			clearLocal();
		} catch (caught) {
			const safeError = safeAuthError(caught);
			if (safeError.status === 401) {
				clearLocal();
			}
			setError(safeError);
			throw safeError;
		}
	}, [clearLocal, client, csrfToken]);

	const value = useMemo<AuthValue>(
		() => ({
			canWrite: status === "authenticated" && csrfToken !== null,
			csrfToken,
			error,
			login,
			logout,
			markAnonymous: clearLocal,
			session,
			status,
		}),
		[clearLocal, csrfToken, error, login, logout, session, status],
	);

	return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
	const value = useContext(AuthContext);
	if (!value) {
		throw new Error("useAuth must be used inside AuthProvider");
	}
	return value;
}

function safeAuthError(caught: unknown): ApiError {
	return caught instanceof ApiError
		? caught
		: new ApiError("Authentication could not be completed.", 0, "AUTH_ERROR");
}

function readCsrf(): string | null {
	try {
		return sessionStorage.getItem(CSRF_STORAGE_KEY);
	} catch {
		return null;
	}
}

function writeCsrf(value: string): void {
	try {
		sessionStorage.setItem(CSRF_STORAGE_KEY, value);
	} catch {
		// The current in-memory session remains usable in this tab.
	}
}

function removeCsrf(): void {
	try {
		sessionStorage.removeItem(CSRF_STORAGE_KEY);
	} catch {
		// Storage may be unavailable; in-memory state is still cleared.
	}
}
