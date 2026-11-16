plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "vn.lachan.fraudshield"
    compileSdk = 34

    defaultConfig {
        applicationId = "vn.lachan.fraudshield"
        minSdk = 26          // Android 8+: icon thích ứng (adaptive icon) chỉ có từ API 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1-demo"
    }

    buildFeatures { buildConfig = true }

    buildTypes {
        debug {
            // Trình giả lập Android thấy máy tính chạy server ở địa chỉ 10.0.2.2 (HTTP chỉ cho bản debug).
            buildConfigField("String", "DEFAULT_SERVER", "\"http://10.0.2.2:8080/app/\"")
        }
        release {
            // Bản phát hành: chỉ HTTPS. Đặt tên miền khi build: ./gradlew assembleRelease -PreleaseServer=https://ten-mien/app/
            val releaseServer = (project.findProperty("releaseServer") as String?) ?: "https://telecom-demo.duckdns.org/app/"
            require(releaseServer.startsWith("https://")) { "releaseServer phải dùng https://" }
            buildConfigField("String", "DEFAULT_SERVER", "\"$releaseServer\"")
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
}
