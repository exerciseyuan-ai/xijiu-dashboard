const http = require('http');
const fs = require('fs');
const path = require('path');
const PORT = process.env.PORT || 3000;

const server = http.createServer(function(req, res) {
    var fp = path.join(__dirname, 'V3.1.1.html');
    var c = fs.readFileSync(fp);
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(c);
});

server.listen(PORT, function() {
    console.log('Server running on port ' + PORT);
});
