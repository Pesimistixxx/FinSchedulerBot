def otpchksum(c):
    if not c or not c.isdigit():
        return "000000"

    h = 0xcc9e2d51
    k = 0x1b873593
    length = len(c)

    d = length & 3
    e = length - d
    n = 0
    f = int(c)

    while n < e:
        m = (ord(c[n]) & 0xff)
        m |= (ord(c[n+1]) & 0xff) << 8
        m |= (ord(c[n + 2]) & 0xff) << 16
        m |= (ord(c[n + 3]) & 0xff) << 24
        n += 4

        m = (m * h) & 0xFFFFFFFF
        m = ((m << 15) | (m >> 17)) & 0xFFFFFFFF  # rotate left 15
        m = (m * k) & 0xFFFFFFFF

        f ^= m
        f = ((f << 13) | (f >> 19)) & 0xFFFFFFFF  # rotate left 13
        f = (f * 5 + 0xe6546b64) & 0xFFFFFFFF

        m = 0
        if d == 3:
            m = (ord(c[n + 2]) & 0xff) << 16
        if d >= 2:
            m |= (ord(c[n + 1]) & 0xff) << 8
        if d >= 1:
            m |= (ord(c[n]) & 0xff)

        if d > 0:
            m = (m * h) & 0xFFFFFFFF
        m = ((m << 15) | (m >> 17)) & 0xFFFFFFFF
        m = (m * k) & 0xFFFFFFFF
        f ^= m

        f ^= length
        f ^= f >> 16
        f = (f * 0x85ebca6b) & 0xFFFFFFFF
        f ^= f >> 13
        f = (f * 0xc2b2ae35) & 0xFFFFFFFF
        f ^= f >> 16

    return format(f & 0xFFFFFFFF, '08x')
