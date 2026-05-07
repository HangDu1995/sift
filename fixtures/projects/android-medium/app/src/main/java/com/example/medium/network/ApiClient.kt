package com.example.medium.network

import com.example.medium.model.User
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Field
import retrofit2.http.FormUrlEncoded
import retrofit2.http.POST

interface ApiService {
    @FormUrlEncoded
    @POST("auth/login")
    fun login(
        @Field("username") username: String,
        @Field("password") password: String
    ): retrofit2.Call<User>
}

class ApiClient private constructor() {

    companion object {
        val instance: ApiClient by lazy { ApiClient() }
    }

    private val client: OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(
            HttpLoggingInterceptor().apply {
                level = HttpLoggingInterceptor.Level.BODY
            }
        )
        .build()

    private val retrofit: Retrofit = Retrofit.Builder()
        .baseUrl("https://api.example.com/v1/")
        .client(client)
        .addConverterFactory(GsonConverterFactory.create())
        .build()

    private val api: ApiService = retrofit.create(ApiService::class.java)

    fun login(username: String, password: String, callback: (Boolean) -> Unit) {
        try {
            val response = api.login(username, password).execute()
            callback(response.isSuccessful)
        } catch (e: Exception) {
            callback(false)
        }
    }
}
