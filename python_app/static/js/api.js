const BASE_URL = '/api';

function getToken() {
    return localStorage.getItem('token');
}

async function apiCall(endpoint, method = 'GET', body = null) {
    const headers = {
        'Content-Type': 'application/json'
    };
    
    const token = getToken();
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }

    const options = { method, headers, cache: 'no-store' };
    if (body) {
        options.body = JSON.stringify(body);
    }

    const res = await fetch(`${BASE_URL}${endpoint}`, options);
    if (res.status === 401) {
        localStorage.clear();
        window.location.href = '/index.html';
        return;
    }
    
    if (res.headers.get('Content-Disposition')) {
        return res; // Return raw response for file downloads
    }
    
    const data = await res.json();
    if (!res.ok) {
        let msg = data.detail || 'API Error';
        if (Array.isArray(msg) && msg.length > 0 && msg[0].msg) {
            msg = msg[0].msg;
        } else if (typeof msg === 'object') {
            msg = JSON.stringify(msg);
        }
        throw new Error(msg);
    }
    return data;
}

function showToast(msg) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, 3000);
}

function logout() {
    localStorage.clear();
    window.location.href = '/index.html';
}
