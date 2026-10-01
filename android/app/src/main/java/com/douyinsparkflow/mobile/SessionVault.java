package com.douyinsparkflow.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Stores the website session only as AES-GCM ciphertext under an Android Keystore key. */
final class SessionVault {
    private static final String PREFS = "notification_state";
    private static final String KEY_ALIAS = "douyinsparkflow.notification.v1";
    private static final String SESSION = "session_ciphertext";
    private static final String LAST_RUN = "last_run_ciphertext";
    private static final String BASELINED = "run_baseline_initialized";

    private SessionVault() {}

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        java.security.Key existing = store.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) return (SecretKey) existing;
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setUserAuthenticationRequired(false)
                .build());
        return generator.generateKey();
    }

    private static String encrypt(String value) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] iv = cipher.getIV();
        byte[] encrypted = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        byte[] packed = new byte[iv.length + encrypted.length];
        System.arraycopy(iv, 0, packed, 0, iv.length);
        System.arraycopy(encrypted, 0, packed, iv.length, encrypted.length);
        return Base64.encodeToString(packed, Base64.NO_WRAP);
    }

    private static String decrypt(String value) throws Exception {
        byte[] packed = Base64.decode(value, Base64.NO_WRAP);
        if (packed.length < 29) throw new IllegalArgumentException("Invalid encrypted value");
        byte[] iv = new byte[12];
        byte[] encrypted = new byte[packed.length - iv.length];
        System.arraycopy(packed, 0, iv, 0, iv.length);
        System.arraycopy(packed, iv.length, encrypted, 0, encrypted.length);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
        return new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
    }

    static void saveSession(Context context, String cookie) {
        try {
            prefs(context).edit().putString(SESSION, encrypt(cookie)).apply();
        } catch (Exception ignored) {
            clearSession(context);
        }
    }

    static String readSession(Context context) {
        String cipherText = prefs(context).getString(SESSION, "");
        if (cipherText.isEmpty()) return "";
        try {
            return decrypt(cipherText);
        } catch (Exception ignored) {
            clearSession(context);
            return "";
        }
    }

    static void clearSession(Context context) {
        prefs(context).edit().remove(SESSION).apply();
    }

    static void clearAccountState(Context context) {
        prefs(context).edit()
                .remove(SESSION)
                .remove(LAST_RUN)
                .remove(BASELINED)
                .apply();
    }

    static String lastRun(Context context) {
        String cipherText = prefs(context).getString(LAST_RUN, "");
        if (cipherText.isEmpty()) return "";
        try { return decrypt(cipherText); } catch (Exception ignored) { return ""; }
    }

    static void setLastRun(Context context, String signature) {
        try { prefs(context).edit().putString(LAST_RUN, encrypt(signature)).putBoolean(BASELINED, true).apply(); }
        catch (Exception ignored) { }
    }

    static boolean hasBaseline(Context context) {
        return prefs(context).getBoolean(BASELINED, false);
    }
}
