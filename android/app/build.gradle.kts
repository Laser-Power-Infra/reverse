plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.laserpower.rtether"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.laserpower.rtether"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    // One stable key for every build, so updates install over earlier versions on every
    // phone. It is a team-internal key committed to the repo, not a Play Store key.
    signingConfigs {
        create("team") {
            storeFile = file("../keystore/rtether.keystore")
            storePassword = "android"
            keyAlias = "rtether"
            keyPassword = "android"
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("team")
        }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("team")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    testImplementation("junit:junit:4.13.2")
}
