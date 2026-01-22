import { supabase } from './lib/supabase.js'

// Проверяем авторизацию
const { data: { user } } = await supabase.auth.getUser()
if (!user) {
    window.location.href = '/login.html'
}

// Показываем информацию о пользователе
document.getElementById('user-info').innerHTML = `
    <p>Email: ${user.email}</p>
    <button onclick="logout()">Выйти</button>
`

async function logout() {
    await supabase.auth.signOut()
    window.location.href = '/login.html'
}