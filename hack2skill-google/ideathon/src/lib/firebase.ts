import { getApp, getApps, initializeApp } from "firebase/app";
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signOut as firebaseSignOut,
} from "firebase/auth";
import {
  getToken,
  initializeAppCheck,
  ReCaptchaEnterpriseProvider,
  ReCaptchaV3Provider,
} from "firebase/app-check";
import firebaseConfig from "../../firebase-applet-config.json";

type AppCheckConfig = typeof firebaseConfig & {
  recaptchaEnterpriseSiteKey?: string;
};

const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApp();
const appCheckConfig = firebaseConfig as AppCheckConfig;

export const auth = getAuth(app);
export const googleProvider = new GoogleAuthProvider();

let appCheckReady: Promise<ReturnType<typeof initializeAppCheck>> | undefined;

function initialiseAppCheck(): Promise<ReturnType<typeof initializeAppCheck>> {
  if (appCheckReady) return appCheckReady;

  const enterpriseKey = appCheckConfig.recaptchaEnterpriseSiteKey?.trim();
  const v3Key = appCheckConfig.recaptchaSiteKey?.trim();
  if (!enterpriseKey && !v3Key) {
    return Promise.reject(
      new Error("App Check is not configured. Add a reCAPTCHA site key before submitting a reflection."),
    );
  }

  appCheckReady = Promise.resolve().then(() =>
    initializeAppCheck(app, {
      provider: enterpriseKey
        ? new ReCaptchaEnterpriseProvider(enterpriseKey)
        : new ReCaptchaV3Provider(v3Key!),
      isTokenAutoRefreshEnabled: true,
    }),
  );
  return appCheckReady;
}

export async function signInWithGoogle() {
  googleProvider.setCustomParameters({ prompt: "select_account" });
  const result = await signInWithPopup(auth, googleProvider);
  return result.user;
}

export async function signOut() {
  await firebaseSignOut(auth);
}

/** Returns a Firebase-issued App Check token. There is deliberately no mock token path. */
export async function getAppCheckToken(): Promise<string> {
  const appCheck = await initialiseAppCheck();
  const token = await getToken(appCheck, false);
  if (!token.token) {
    throw new Error("App Check could not attest this browser. Please reload and try again.");
  }
  return token.token;
}
