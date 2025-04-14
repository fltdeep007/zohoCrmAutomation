// Add this to a new file: static/js/auth.js

document.addEventListener('DOMContentLoaded', function() {
    // Toggle user dropdown
    const userDropdown = document.getElementById('userDropdown');
    if (userDropdown) {
        new bootstrap.Dropdown(userDropdown);
    }
    
    // Check authentication status on page load
    function checkAuthStatus() {
        fetch('/api/user/info')
            .then(response => response.json())
            .then(data => {
                if (data.logged_in) {
                    console.log('User is logged in:', data.user_info.name);
                    
                    // Any additional actions needed when user is confirmed logged in
                    const authRequiredElements = document.querySelectorAll('.auth-required');
                    authRequiredElements.forEach(el => {
                        el.classList.remove('d-none');
                    });
                    
                    const noAuthElements = document.querySelectorAll('.no-auth-only');
                    noAuthElements.forEach(el => {
                        el.classList.add('d-none');
                    });
                } else {
                    console.log('User is not logged in');
                    
                    // Any additional actions needed when user is confirmed logged out
                    const authRequiredElements = document.querySelectorAll('.auth-required');
                    authRequiredElements.forEach(el => {
                        el.classList.add('d-none');
                    });
                    
                    const noAuthElements = document.querySelectorAll('.no-auth-only');
                    noAuthElements.forEach(el => {
                        el.classList.remove('d-none');
                    });
                }
            })
            .catch(error => {
                console.error('Error checking auth status:', error);
            });
    }
    
    // Run auth check
    checkAuthStatus();
});