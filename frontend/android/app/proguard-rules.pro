# Add project specific ProGuard rules here.
# By default, the flags in this file are appended to flags specified
# in /usr/local/Cellar/android-sdk/24.3.3/tools/proguard/proguard-android.txt
# You can edit the include path and order by changing the proguardFiles
# directive in build.gradle.
#
# For more details, see
#   http://developer.android.com/guide/developing/tools/proguard.html

# react-native-reanimated
-keep class com.swmansion.reanimated.** { *; }
-keep class com.facebook.react.turbomodule.** { *; }

# Add any project specific keep options here:

# --- R8 keep rules (#890) ---------------------------------------------------
# Libraries that ship their own consumer rules (not repeated here): expo,
# expo-modules-core, expo-image (Glide), expo-camera, react-native-screens,
# react-native-svg, react-native-reanimated, react-native-worklets, and the
# AARs for React Native, Hermes, Fresco, OkHttp and sentry-android. Shipping no
# rules: react-native-webview, @sentry/react-native's own Android module,
# react-native-gesture-handler, safe-area-context, async-storage, netinfo,
# expo-secure-store, expo-image-manipulator, expo-crypto, expo-file-system,
# expo-font, expo-haptics, expo-web-browser. See docs/claude/android-ci.md.

# react-native-webview (Turnstile runs in a WebView). The page talks to the app
# through RNCWebViewBridge via addJavascriptInterface, and the web side finds
# those methods by name, so R8 must neither strip nor rename them.
-keepattributes JavascriptInterface
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keep class com.reactnativecommunity.webview.** { *; }

# Sentry: readable native stack traces and a mapping file that matches. The
# Sentry Android Gradle plugin uploads the mapping for release builds.
-keepattributes LineNumberTable,SourceFile
-renamesourcefileattribute SourceFile
-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod
