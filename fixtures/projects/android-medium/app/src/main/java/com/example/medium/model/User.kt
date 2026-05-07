package com.example.medium.model

import com.google.gson.annotations.SerializedName

data class User(
    @SerializedName("id") val id: String,
    @SerializedName("username") val username: String,
    @SerializedName("display_name") val displayName: String,
    @SerializedName("email") val email: String,
    @SerializedName("avatar_url") val avatarUrl: String?
) {
    val initials: String
        get() = displayName.split(" ").map { it.first() }.joinToString("").take(2)
}
