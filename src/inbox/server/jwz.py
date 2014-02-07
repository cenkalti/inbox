#!/usr/bin/python

import jwzthreading
import rfc822

msglist = []

# M1:
f = open('thread1.txt')
rfc1 = rfc822.Message(f)
f.close()

m1 = jwzthreading.make_message(rfc1)
msglist.append(m1)

# M2:
f = open('thread2.txt')
rfc2 = rfc822.Message(f)
f.close()

m2 = jwzthreading.make_message(rfc2)
msglist.append(m2)

# # M3:
# f = open('thread3.txt')
# rfc3 = rfc822.Message(f)
# f.close()

# m3 = jwzthreading.make_message(rfc3)
# msglist.append(m3)

# # M4:
# f = open('non_thread.txt')
# rfc4 = rfc822.Message(f)
# f.close()

# m4 = jwzthreading.make_message(rfc4)
# msglist.append(m4)

# Thread:
subject_table = jwzthreading.thread(msglist)
items = subject_table.items()

print "items = ", items
print "\n\n"
for subj, container in items:
	print "subj = ", subj
	jwzthreading.print_container(container, debug=0)
